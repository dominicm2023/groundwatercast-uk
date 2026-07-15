# Contributing to GroundwaterCast UK

Thanks for your interest in the project. GroundwaterCast is an independent
open-source project; contributions of all kinds are welcome — bug reports,
documentation fixes, code, and hydrological insight.

## Reporting problems

Open a [GitHub issue](https://github.com/dominicm2023/groundwatercast-uk/issues) with:

- what you expected and what happened instead;
- steps to reproduce, if you can (Python version, OS, the command you ran);
- any error output.

Forecast-quality observations are welcome too — if a published forecast for a
borehole or gauge you know looks wrong, an issue with the station and date is
genuinely useful while the verification archive matures.

## Seeking support

For questions that aren't bugs — getting the pipeline running, understanding
the methodology, interpreting outputs — open a
[GitHub discussion](https://github.com/dominicm2023/groundwatercast-uk/discussions)
or an issue. The methodology itself is documented in
[`docs/model.md`](docs/model.md) and
[`docs/ensemble_forecast_design.md`](docs/ensemble_forecast_design.md).

## Contributing code

1. **Open an issue first** for anything non-trivial, so the approach can be
   agreed before you invest time.
2. Fork, branch, and make your change.
3. **Run the test suite** — it runs standalone, no downloaded data needed:

   ```bash
   pip install -r requirements.txt
   python -m pytest -q
   ```

4. **Add tests** for new behaviour or fixed bugs.
5. Open a pull request describing what changed and why. CI runs the suite on
   every push and PR.

### House rules

The same rules the existing code follows:

- **Config-driven** — user-facing choices live in `config/config.json` and
  `data/thresholds/`, not hardcoded.
- **No leakage** — train/test splits are time-based, never shuffled.
- **Auditability** — raw API responses are cached; derived artefacts are
  rebuildable from them.
- **Honest uncertainty** — forecast output carries its caveats with it
  (uncalibrated bands, experimental badges, disclaimers). Changes that would
  overstate skill won't be merged.

## Code of conduct

Be respectful and constructive. Critique code and methods, not people.
Harassment or personal attacks are not tolerated.
