IMAGE ?= blast-radius-entry
SAMPLE := $(CURDIR)/kit/sample
OUTPUT := $(CURDIR)/output.jsonl

# The flags the scorer runs the image with (from the starter kit).
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: setup map local validate build sample

# One-time: pinned sqlglot checkout + Python 3.13 venv with the scorer's packages.
setup:
	mkdir -p .work
	test -d .work/sqlglot || (git clone -q https://github.com/tobymao/sqlglot.git .work/sqlglot \
		&& git -C .work/sqlglot checkout -q eb11aec4be8cb2122afe713a4816662c3f0fb2fa)
	test -d .work/venv || uv venv -q -p 3.13 .work/venv
	VIRTUAL_ENV=$(CURDIR)/.work/venv uv pip install -q -r kit/requirements.lock coverage

# Rebuild map/ (runs the whole sqlglot suite once under coverage, ~1 minute).
map:
	tools/make_map.sh

# Run the selector natively on the 30 sample commits and score it (no Docker).
local:
	tools/run_local.sh

# Score on synthetic commits (tools/mutate.py + tools/truth.py write .work/val).
validate:
	tools/run_local.sh $(CURDIR)/.work/val

# Docker, exactly like the scorer.
build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 kit/score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)
