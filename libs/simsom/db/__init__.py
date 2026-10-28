# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Database boundary for SimSoM.

The DB layer is split by process ownership:
- shared core: `db/core/`
- DB clock integration docs: `db/clock/`
- controller DB client
- worker DB client
- policy DB client
- analytics DB client

Process components should import only their own DB client plus `db.core`.
"""
