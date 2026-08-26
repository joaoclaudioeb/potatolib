PROJECT ?= ../

build:
	cd gen && python3 potatolib.py all

verify: build
	git diff --exit-code symbols

check:
	cd gen && python3 potatolib.py all --dry-run

tables:
	@test -n "$(PROJECT)" || { echo "usage: make tables PROJECT=<path to the main project>"; exit 1; }
	cd gen && python3 potatolib.py updt_tables --project "$(abspath $(PROJECT))"

.PHONY: build verify check tables
