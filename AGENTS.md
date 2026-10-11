# Eylema repository adapter

Read the [shared contributor contract](https://github.com/eylema-dev/devguide/blob/main/AGENTS.md) and [workflow](https://github.com/eylema-dev/devguide/blob/main/CONTRIBUTING.md) before changing files. This adapter adds no competing policy. If the shared contract cannot be obtained, report that limitation before changing policy-sensitive files.

Repository: `eylema-dev/eylema-evals`. Read [README](README.md), [repository scope](repository.json), the [repository map](https://github.com/eylema-dev/devguide/blob/main/docs/en/repository-map.md), and the accepted task. Work on one scoped branch from current `main`, preserve unrelated work and deliver through a PR. Repository creation does not authorize merge or deployment. Record actual tools/models, evidence and pending independent review.

English is canonical; Turkish documentation tracks its source. Code, identifiers, templates and technical records use English. No runtime is implemented by this setup. The exact local documentation-stage commands are:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r tools/requirements-ci.txt
.venv/bin/python -m unittest discover -s tools/tests -v
.venv/bin/python tools/check_repository.py
.venv/bin/python tools/check_pr.py --body-file .github/PULL_REQUEST_TEMPLATE.md --allow-template
git diff --check
```

Refresh reviewed bytes with `.venv/bin/python tools/check_repository.py --write-translation-hashes`. Cross-repository translation, API, roadmap and link verification runs in devguide against pinned commits; update that repository set whenever a member changes. Local green CI alone is not cross-repository acceptance.
