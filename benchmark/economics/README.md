# Economics and storage overlays

This package adds an orthogonal resource overlay for scale and BEAM reports. It
must not be interpreted as a quality score or a provider billing statement.

Measured locally:

- SQLite main/WAL/SHM bytes;
- bytes per entity;
- entity/history/journal/link counts;
- deterministic character-based token proxies;
- optional token cost estimates only when explicit input/output prices are supplied.

`token_proxy()` is intentionally labeled a proxy. Provider-backed runs must
replace it with provider-reported token counts and record the tokenizer/model
configuration in the control profile.

The current helpers are tested independently and are ready to be called by the
existing scale/BEAM runners. No canonical scale or BEAM result is changed by
this package yet.

```bash
python3 -m unittest discover -s benchmark/economics -p 'test_*.py' -v
```
