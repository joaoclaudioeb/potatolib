PROJECT ?= ../
MODELS_DIR ?= 3dmodels
MODELS_TAG ?= 3dshapes

build:
	cd gen && python3 potatolib.py all

verify: build
	git diff --exit-code symbols

check:
	cd gen && python3 potatolib.py all --dry-run

tables:
	@test -n "$(PROJECT)" || { echo "usage: make tables PROJECT=<path to the main project>"; exit 1; }
	cd gen && python3 potatolib.py updt_tables --project "$(abspath $(PROJECT))"

3d:
	python3 gen/sync3d.py pull --dir "$(MODELS_DIR)" --tag "$(MODELS_TAG)"

3d-push:
	python3 gen/sync3d.py push --dir "$(MODELS_DIR)" --tag "$(MODELS_TAG)" $(if $(PRUNE),--prune)

3d-status:
	python3 gen/sync3d.py status --dir "$(MODELS_DIR)" --tag "$(MODELS_TAG)"

.PHONY: build verify check tables 3d 3d-push 3d-status
