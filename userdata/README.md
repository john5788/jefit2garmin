# userdata

Your files live here, and nothing in this folder except this README is part of the project:

| | |
|---|---|
| `config.toml` | your settings and passwords (the first run of `jefit2garmin` creates it) |
| `exercise_map.toml` | your Jefit → Garmin exercise matches; edit freely |
| `logs/` | `jefit2garmin.log`: what each run did; rotated at 1 MB, last 3 kept |
| `state/` | jefit2garmin's own files: the sync record, caches, `garmin_exercises.md`, Garmin login. Don't edit; `jefit2garmin --reset` deletes it to start over. |

Keep this folder when you update jefit2garmin, and keep it private: `config.toml` holds your
passwords.
