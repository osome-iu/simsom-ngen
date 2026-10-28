<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

## Place for calibration scripts

**Qualification**
- Using empirical data
- Can be reused by other researchers

**Folder Structure**

Save the script under the appropriate component folder (e.g. `clock/`), create new folder(s) as needed.

**Content Requirement:**
- What data it's using

    _e.g. Bluesky firehose's account following activities between `2025-01-01` and `2025-02-02`_
          https://docs.bsky.app/docs/advanced-guides/firehose
- What new feature it's contributing

    _e.g. Adds a time distribution for how often an account follows another account, and introduces a new clock class, `SimpleClockManager`, which generates integer timestamps and IDs that increment by 1._
    /libs/simsom/clock_manager.py#L22-L38
