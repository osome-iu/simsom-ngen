<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

## Lurker Ratio

This directory contains scripts used to calibrate the `setup_db.lurker_ratio`
default in `libs/simsom/config/default_simulator_config.jsonc`.

The main report is `lurker_ratio_report.ipynb`, which analyzes the generated
results from running `lurker_ratio_prep.py`.

Current report defaults:

- Analysis range: `2023-09` through `2026-04`
- Raw-data discovery: recursive scan under a configurable data path
- Day filter: keep days with at least `22` hours of coverage instead of requiring strict full UTC days
- Seen-user note: `app.bsky.actor.profile` `create` and `delete` records are ignored for `seen_users`
- User categories:
  1. `non_lurker`: observed user with at least one `create` whose raw type is `app.bsky.feed.post` or `app.bsky.feed.repost`
  2. `lurker`: observed user with no `create` whose raw type is `app.bsky.feed.post` or `app.bsky.feed.repost`
- Raw-data note: the split only checks raw type `app.bsky.feed.post` or `app.bsky.feed.repost` on `create`
