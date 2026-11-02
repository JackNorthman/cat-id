"""
cat-id — who is it, from the embeddings Frigate already computes.

Frigate's semantic search stores a 768-d JinaCLIP vector for every tracked object.
Those vectors separate two resident cats from a visiting lookalike better than the
COCO detector separates "cat" from "dog", so identity is a nearest-CENTROID lookup
against the crops labelled in clips/<Name>/dataset/<class>/.

Centroid, not k-NN: majority-vote k-NN scored 0/7 on the lookalike purely because
the resident cat outnumbered it 54:7. Averaging per class removes that imbalance -> 6/7.
"""
import json, logging, os, re, sqlite3, sys, threading, time
import numpy as np
import paho.mqtt.client as mqtt

LOG = logging.getLogger("cat-id")
DB = os.environ.get("FRIGATE_DB", "/frigate-config/frigate.db")
DATASET = os.environ.get("DATASET_DIR", "/dataset")
LABELS = [l.strip() for l in os.environ.get("LABELS", "cat,dog,bird").split(",") if l.strip()]
MIN_SCORE = float(os.environ.get("MIN_SCORE", "0.55"))
MIN_MARGIN = float(os.environ.get("MIN_MARGIN", "0.01"))
TOPIC = os.environ.get("TOPIC_PREFIX", "catid")
FRIGATE_TOPIC = os.environ.get("FRIGATE_TOPIC_PREFIX", "frigate")
DISCOVERY = os.environ.get("HA_DISCOVERY_PREFIX", "homeassistant")
DIM = 768
EVID = re.compile(r"(\d{9,}\.\d+-[A-Za-z0-9]+)")


def load_vectors():
    """Read sqlite-vec's shadow tables directly. The vec0 virtual table needs an
    extension we do not have, but the storage underneath is plain SQL."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    try:
        loc = {i: (c, o) for i, c, o in
               con.execute("select id, chunk_id, chunk_offset from vec_thumbnails_rowids")}
        blobs = {r: v for r, v in
                 con.execute("select rowid, vectors from vec_thumbnails_vector_chunks00")}
    finally:
        con.close()
    return loc, blobs


def vector_for(event_id, loc, blobs):
    hit = loc.get(event_id)
    if not hit:
        return None
    chunk, off = hit
    blob = blobs.get(chunk)
    if blob is None:
        return None
    raw = blob[off * DIM * 4:(off + 1) * DIM * 4]
    if len(raw) != DIM * 4:
        return None
    v = np.frombuffer(raw, dtype=np.float32).astype(np.float64)
    n = np.linalg.norm(v)
    return v / n if n else None


def event_exists(event_id):
    """Frigate publishes an MQTT `end` for objects it then DISCARDS, so a missing
    embedding is usually a missing event, not a failure. Check before crying wolf."""
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
        try:
            return con.execute("select 1 from event where id=?", (event_id,)).fetchone() is not None
        finally:
            con.close()
    except Exception:
        return True  # can't tell -> keep the warning


def dataset_ids():
    """class -> [event_id]. The dataset folders ARE the label store, so anything
    labelled in Frigate's own UI is picked up on the next rebuild."""
    out = {}
    if not os.path.isdir(DATASET):
        return out
    for cls in sorted(os.listdir(DATASET)):
        d = os.path.join(DATASET, cls)
        if not os.path.isdir(d) or cls == "temp":
            continue
        ids = []
        for fn in os.listdir(d):
            m = EVID.search(fn)
            if m:
                ids.append(m.group(1))
        if ids:
            out[cls] = ids
    return out


class Model:
    def __init__(self):
        self.centroids = {}
        self.counts = {}
        self.built = 0.0
        self.lock = threading.Lock()

    def rebuild(self):
        loc, blobs = load_vectors()
        cents, counts = {}, {}
        for cls, ids in dataset_ids().items():
            vecs = [v for v in (vector_for(i, loc, blobs) for i in set(ids)) if v is not None]
            if not vecs:
                LOG.warning("class %r has no usable embeddings (crops too old?)", cls)
                continue
            m = np.mean(vecs, axis=0)
            n = np.linalg.norm(m)
            if not n:
                continue
            cents[cls] = m / n
            counts[cls] = len(vecs)
        with self.lock:
            self.centroids, self.counts, self.built = cents, counts, time.time()
        LOG.info("centroids rebuilt: %s", counts or "NONE - label some crops first")
        return counts

    def classify(self, vec):
        with self.lock:
            cents = dict(self.centroids)
        if not cents or vec is None:
            return None
        scored = sorted(((float(vec @ c), name) for name, c in cents.items()), reverse=True)
        best_score, best = scored[0]
        second = scored[1][0] if len(scored) > 1 else -1.0
        margin = best_score - second
        ok = best_score >= MIN_SCORE and margin >= MIN_MARGIN
        return {
            "identity": best if ok else "unknown",
            "score": round(best_score, 4),
            "margin": round(margin, 4),
            "runner_up": scored[1][1] if len(scored) > 1 else None,
            "confident": ok,
            "all": {n: round(s, 4) for s, n in scored},
        }


MODEL = Model()


def known_cameras():
    """Cameras that have ever produced a tracked object we care about, so the HA
    entities exist before the first cat walks past rather than after."""
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
        try:
            q = ",".join("?" * len(LABELS))
            return [r[0] for r in con.execute(
                f"select distinct camera from event where label in ({q})", LABELS)]
        finally:
            con.close()
    except Exception:
        LOG.exception("could not list cameras")
        return []


def discovery(client, camera):
    slug = re.sub(r"[^a-z0-9_]", "_", camera.lower())
    client.publish(
        f"{DISCOVERY}/sensor/catid_{slug}/config",
        json.dumps({
            # named per camera: one shared name gives sensor.cat_id_cat and ..._cat_2
            "name": camera.replace("_", " ").title(),
            "unique_id": f"catid_{slug}",
            "state_topic": f"{TOPIC}/{camera}/state",
            "json_attributes_topic": f"{TOPIC}/{camera}/attributes",
            "icon": "mdi:cat",
            "device": {
                "identifiers": ["catid"],
                "name": "Cat ID",
                "manufacturer": "cat-id",
                "model": "Frigate embeddings, nearest-centroid",
            },
        }), retain=True)


SEEN = set()


def handle(client, payload):
    if payload.get("type") != "end":
        return
    after = payload.get("after") or {}
    label, eid, cam = after.get("label"), after.get("id"), after.get("camera")
    if label not in LABELS or not eid or eid in SEEN:
        return
    SEEN.add(eid)

    # the thumbnail embedding is written when the event ends - give it a moment
    res = None
    for attempt in range(6):
        loc, blobs = load_vectors()
        v = vector_for(eid, loc, blobs)
        if v is not None:
            res = MODEL.classify(v)
            break
        time.sleep(2 * (attempt + 1))
    if res is None:
        if event_exists(eid):
            LOG.warning("no embedding for %s (%s on %s) after retries", eid, label, cam)
        else:
            LOG.info("%s (%s on %s) was discarded by Frigate - nothing to classify", eid, label, cam)
        return

    LOG.info("%s %s on %s -> %s (%.3f, margin %.3f)",
             eid, label, cam, res["identity"], res["score"], res["margin"])
    discovery(client, cam)
    client.publish(f"{TOPIC}/{cam}/state", res["identity"], retain=True)
    client.publish(f"{TOPIC}/{cam}/attributes",
                   json.dumps({**res, "event_id": eid, "detector_label": label,
                               "camera": cam, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}),
                   retain=True)


def selftest():
    """Leave-one-out over the labelled dataset - the deployed code, not a notebook."""
    loc, blobs = load_vectors()
    X, y = [], []
    for cls, ids in dataset_ids().items():
        for i in set(ids):
            v = vector_for(i, loc, blobs)
            if v is not None:
                X.append(v); y.append(cls)
    if not X:
        print("no labelled crops with embeddings"); return 1
    X = np.array(X); y = np.array(y)
    classes = sorted(set(y))
    correct = 0
    per = {c: [0, 0] for c in classes}
    for i in range(len(X)):
        scores = {}
        for c in classes:
            m = (y == c).copy(); m[i] = False
            if not m.any():
                continue
            v = X[m].mean(0); n = np.linalg.norm(v)
            if n: scores[c] = float(X[i] @ (v / n))
        if not scores:
            continue
        pred = max(scores, key=scores.get)
        per[y[i]][1] += 1
        if pred == y[i]:
            correct += 1; per[y[i]][0] += 1
    print(f"leave-one-out over {len(X)} labelled crops")
    print(f"  overall {correct}/{len(X)} = {100*correct/len(X):.0f}%")
    for c in classes:
        ok, tot = per[c]
        print(f"    {c:10} {ok}/{tot}")
    return 0


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    MODEL.rebuild()

    def rebuilder():
        while True:
            time.sleep(int(os.environ.get("REBUILD_SECONDS", "900")))
            try:
                MODEL.rebuild()
            except Exception:
                LOG.exception("centroid rebuild failed")
    threading.Thread(target=rebuilder, daemon=True).start()

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id=os.environ.get("MQTT_CLIENT_ID", "cat-id"))
    user, pw = os.environ.get("MQTT_USER"), os.environ.get("MQTT_PASSWORD")
    if user:
        c.username_pw_set(user, pw)

    def on_connect(cl, _u, _f, rc, _p=None):
        LOG.info("mqtt connected rc=%s", rc)
        cl.subscribe(f"{FRIGATE_TOPIC}/events")
        for cam in known_cameras():
            discovery(cl, cam)
            # seed a state so the entity is present, not unavailable, before the first cat
            cl.publish(f"{TOPIC}/{cam}/state", "unknown", retain=True)
        LOG.info("announced cameras: %s", known_cameras())

    def on_message(cl, _u, msg):
        try:
            handle(cl, json.loads(msg.payload.decode()))
        except Exception:
            LOG.exception("failed handling message")

    c.on_connect, c.on_message = on_connect, on_message
    c.connect(os.environ.get("MQTT_HOST", "localhost"),
              int(os.environ.get("MQTT_PORT", "1883")), 60)
    c.loop_forever()


if __name__ == "__main__":
    main()
