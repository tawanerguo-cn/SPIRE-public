# Instrument profiles and observations

The profiles in this directory describe the current STM/Nanonis implementation and historical observations from one installation. They are examples for adaptation; check every site-specific signal name, limit, driver, and timing field against the target instrument and its approved operating procedure.

| Topic | Reference |
| --- | --- |
| Recorder signal and gate settings | [Example station profile](spire_local_station.toml) |
| Home, Withdraw, and feedback recovery | [Conditioning recovery](device1_conditioning_recovery.md) |
| Scan timing and state readback | [Scan timing](device1_scan_timing.md) |
| Spectroscopy readback | [Spectroscopy](device1_spectroscopy_readback.md) |
| Drift compensation | [Drift compensation](nanonis_drift_compensation.md) |

The named profile uses the loader's existing schema and default ID so software and offline fixtures remain compatible. Its original sample records, endpoint data, session paths, and raw acquisition references have been removed. It is not a validated configuration for a new laboratory. Live use requires a locally configured connection and independent commissioning.

When diagnosing motion-related noise, distinguish stopping motion, closing the control view, and disabling the driver output. Keep working point, acquisition, and display averaging consistent during comparisons. A single noise change after switching a driver state is not by itself enough to identify the cause.
