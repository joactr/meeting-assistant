set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

bootstrap:
    python tooling/run.py bootstrap

generate:
    python tooling/run.py generate

check:
    python tooling/run.py check

test:
    python tooling/run.py test

smoke:
    python tooling/run.py smoke

audit:
    python tooling/run.py audit

build:
    python tooling/run.py build

package:
    python tooling/run.py package

release-check:
    python tooling/run.py release-check

dev:
    python tooling/run.py dev

dev-engine:
    python tooling/run.py dev-engine

fetch-model model:
    python tooling/run.py fetch-model {{model}}
