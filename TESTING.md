# Testing

On GitHub: Actions → Tests and demo → Run workflow → main → Run workflow. Inspect each Python matrix job's test and synthetic demo steps.

Locally:

```sh
python -m unittest discover -s tests -v
python demo.py
```

No real provider calls are made by the sample. Read README.md for supported runtimes and limits.
