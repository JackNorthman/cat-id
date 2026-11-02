# cat-id

**Name your individual pets from Frigate's own embeddings. No training, no Frigate+, no extra model.**

cat-id watches Frigate's MQTT events and, for every finished `cat`, `dog` or `bird` event, works out *which* animal it was ("Tom", "Luna", "the visiting lookalike") and publishes that to Home Assistant as a sensor per camera.

> Unofficial. Not affiliated with, or endorsed by, the Frigate project or Home Assistant.

## Why

Frigate tells you *a cat* walked past. If you have more than one, or a visiting cat that looks like one of yours, you want to know *which* cat. Training a custom classifier is a lot of effort for a handful of animals, and the COCO detector can't even reliably tell a cat from a small dog.

Frigate already does the hard part. With semantic search turned on, it stores a 768-dimension JinaCLIP vector for every tracked object. Those vectors separate individual animals surprisingly well, far better than the detector separates "cat" from "dog". cat-id just reads them.

## How it works

1. **Labels.** You sort a few crops per animal into folders: `clips/<Name>/dataset/<class>/`, for example `clips/Pets/dataset/Tom/` and `clips/Pets/dataset/Luna/`. Those folders *are* the label store. cat-id only reads the Frigate event ID in each file name (`<unix time>.<fraction>-<suffix>`), not the image, so anything you label in Frigate's own UI that lands in those folders counts too.
2. **Centroids.** For each class it looks up the stored embedding of every labelled event, averages them and normalises the result into one centroid per animal. The centroids rebuild every 15 minutes, so new labels are picked up without a restart.
3. **Lookup.** When Frigate publishes an `end` event for a watched label, cat-id fetches that event's embedding (retrying for a few seconds while Frigate writes it) and picks the nearest centroid by cosine similarity.
4. **Publish.** The answer goes to MQTT with Home Assistant discovery: `catid/<camera>/state` plus a JSON `catid/<camera>/attributes`.

It **ignores the detector's label** when it names the animal. A cat that Frigate boxed as a `dog` is still named correctly, and the original label is kept in `detector_label`.

It reads the sqlite-vec shadow tables (`vec_thumbnails_rowids`, `vec_thumbnails_vector_chunks00`) in `frigate.db` with plain SQLite, so it needs no sqlite-vec extension and never writes to the database.

## Requirements

- Frigate with **semantic search enabled** and the default Jina model, which gives 768-d vectors. If your model's vectors have a different size, change `DIM` in `app/main.py`.
- Frigate publishing to an MQTT broker, and Home Assistant using the same broker (if you want the entities).
- Docker with Compose, on a machine that can see Frigate's `config` folder (for `frigate.db`) and its `media/clips` folder.

## Quick start

```sh
git clone https://github.com/JackNorthman/cat-id.git
cd cat-id
cp .env.example .env
$EDITOR .env          # MQTT details + the two Frigate paths
```

Make the label folders and put a few crops of each animal in them. Five to ten per animal is a good start, and more variety (day, night, different cameras) matters more than more of the same:

```
<frigate media>/clips/Pets/dataset/
├── Tom/
│   ├── 1727612345.123456-abc123.webp
│   └── ...
└── Luna/
    └── ...
```

Check how well your labels separate before going live. Example output:

```sh
docker compose run --rm cat-id --selftest
```

```
leave-one-out over 61 labelled crops
  overall 55/61 = 90%
    Luna       18/20
    Tom        37/41
```

Then run it:

```sh
docker compose up -d --build
docker compose logs -f cat-id
```

## Configuration

Set these in `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `MQTT_HOST` | `localhost` | MQTT broker. Inside a container `localhost` is the container itself, so use the broker's host name or IP. |
| `MQTT_PORT` | `1883` | Broker port. |
| `MQTT_USER` / `MQTT_PASSWORD` | unset | Broker credentials, if your broker needs them. |
| `MQTT_CLIENT_ID` | `cat-id` | MQTT client ID. |
| `FRIGATE_CONFIG_DIR` | `./frigate/config` | **Host** folder holding `frigate.db`. Used by `compose.yaml`. |
| `FRIGATE_DATASET_DIR` | `./frigate/media/clips/Pets/dataset` | **Host** label folder, one sub-folder per animal. Used by `compose.yaml`. |
| `FRIGATE_DB` | `/frigate-config/frigate.db` | Path to the database inside the container. |
| `DATASET_DIR` | `/dataset` | Path to the label folder inside the container. |
| `LABELS` | `cat,dog,bird` | Frigate labels to classify (comma-separated). |
| `MIN_SCORE` | `0.55` | Minimum cosine similarity to the best centroid. Below it the state is `unknown`. |
| `MIN_MARGIN` | `0.01` | Minimum gap between the best and second-best centroid. Below it the state is `unknown`. |
| `REBUILD_SECONDS` | `900` | How often the centroids are rebuilt from the label folders. |
| `TOPIC_PREFIX` | `catid` | Prefix for cat-id's own topics. |
| `FRIGATE_TOPIC_PREFIX` | `frigate` | Frigate's MQTT `topic_prefix`. cat-id subscribes to `<prefix>/events`. |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant MQTT discovery prefix. |
| `LOG_LEVEL` | `INFO` | Python log level. |

Every sub-folder of the label folder becomes a class, except one called `temp`. A `none` or `not_a_pet` folder works as a reject class: label things that are not your animals and they will come out as that name rather than as the nearest pet.

## MQTT and Home Assistant

On connect, cat-id announces a sensor for every camera that has ever recorded a watched label and seeds its state to `unknown`, so the entities exist before the first animal walks past.

| Topic | Retained | Payload |
|---|---|---|
| `homeassistant/sensor/catid_<camera>/config` | yes | Discovery config. Sensor named after the camera, grouped under one "Cat ID" device. |
| `catid/<camera>/state` | yes | The class name (for example `Tom`), or `unknown`. |
| `catid/<camera>/attributes` | yes | JSON, below. |

Attributes:

| Key | Meaning |
|---|---|
| `identity` | Same as the state. |
| `score` | Cosine similarity to the winning centroid. |
| `margin` | Winning score minus runner-up score. |
| `runner_up` | Second-closest class. |
| `confident` | `true` if both thresholds passed. |
| `all` | Score for every class. |
| `event_id` | Frigate event ID, handy for linking to the clip. |
| `detector_label` | What Frigate's detector called it (`cat`, `dog`, ...). |
| `camera` | Camera name. |
| `at` | Local time of classification. |

In Home Assistant the entity usually appears as something like `sensor.cat_id_front`. An example automation trigger:

```yaml
trigger:
  - platform: state
    entity_id: sensor.cat_id_front
    to: "Luna"
```

## Gotchas and lessons

- **Centroid beats k-NN.** Majority-vote k-NN scored **0/7** on a visiting lookalike, purely because one resident cat had 54 labelled crops against its 7. Every nearest neighbour was the majority cat. Averaging each class into one centroid removes the imbalance, and that got **6/7** (about 86%) on the lookalike pair. Don't add k-NN back.
- **"no embedding for ..." warnings usually mean Frigate discarded the event.** Frigate publishes an MQTT `end` for some objects it then throws away (too short, false positive and so on), so there is never a vector to read. cat-id checks whether the event still exists and logs those at INFO instead. A warning for an event that *does* exist is worth a look (is semantic search running?).
- **"class has no usable embeddings (crops too old?)"** means every labelled event for that class has been purged by Frigate's retention, and its embedding with it. Label some newer sightings.
- **The clips folder is root-only on a stock Frigate install.** The container runs as root, so a bind mount works. If you browse or script the label folders outside Docker, either use suitable permissions or go through Frigate's own container, for example `docker exec frigate python3 -c "import os; print(os.listdir('/media/frigate/clips'))"`.
- **The database mount is read-write on purpose.** SQLite needs to take a read lock on the WAL file. cat-id opens the database `mode=ro` at the SQL layer and never writes.
- **It does not work for cars.** CLIP embeddings capture "silver hatchback", not "*that* silver hatchback". In testing, the second sighting of the same car ranked 87th of 1,685 candidates. Stick to animals.
- **Run `--selftest` after you change labels.** It is a leave-one-out test that uses the same code as the live path. A class that scores badly usually needs more varied crops, not more of the same.

## Licence

MIT. See [LICENSE](LICENSE).
