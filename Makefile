# `make build` installs. Everything else is a convenience.
#
# Nothing here spells the project's name out either: the command name, the
# man page filename and its @PLACEHOLDERS@ are all read out of
# src/core/branding.py at parse time, so a rebrand needs no edit to this file.

ROOT    := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

# --- knobs, all overridable: `make PREFIX=/usr/local build` -----------------
PYTHON  ?= python3
VENV    ?= $(ROOT)/.venv
PREFIX  ?= $(HOME)/.local
BINDIR  ?= $(PREFIX)/bin
MANDIR  ?= $(PREFIX)/share/man/man1
# Extras to install. `make EXTRAS= build` for a runtime-only install.
EXTRAS  ?= dev

# --- brand, read from the one file that holds it ---------------------------
BRAND       := $(shell $(PYTHON) -c 'import sys; sys.path.insert(0, "$(ROOT)/src"); import core, core.branding as b; print(b.COMMAND, b.SLUG, b.ENV_PREFIX, core.__version__)')
COMMAND     := $(word 1,$(BRAND))
SLUG        := $(word 2,$(BRAND))
ENVPFX      := $(word 3,$(BRAND))
VERSION     := $(word 4,$(BRAND))
# Read one at a time: these may contain spaces, so they cannot be $(word)-split.
APPNAME     := $(shell $(PYTHON) -c 'import sys; sys.path.insert(0, "$(ROOT)/src"); import core.branding as b; print(b.NAME)')
DESCRIPTION := $(shell $(PYTHON) -c 'import sys; sys.path.insert(0, "$(ROOT)/src"); import core.branding as b; print(b.DESCRIPTION.rstrip(".").lower())')
CMD_UPPER   := $(shell printf '%s' '$(COMMAND)' | tr '[:lower:]' '[:upper:]')
TODAY       := $(shell date +%Y-%m-%d)

# The console script's name comes from pyproject.toml, the *other* brand file.
# It has to agree with branding.COMMAND or `build` would link a name that was
# never installed — see the check-brand target.
SCRIPT := $(shell $(PYTHON) -c 'import tomllib; print(next(iter(tomllib.load(open("$(ROOT)/pyproject.toml","rb"))["project"]["scripts"]), ""))')

BUILDDIR := $(ROOT)/build
MANPAGE  := $(BUILDDIR)/$(COMMAND).1
SPEC     := $(if $(EXTRAS),.[$(EXTRAS)],.)
PY       := $(VENV)/bin/python
STAMP    := $(VENV)/.installed
UV       := $(shell command -v uv 2>/dev/null)

.DEFAULT_GOAL := help
.PHONY: help build install uninstall man test lint clean distclean check-brand

help:  ## show this
	@printf '\n  %s %s\n\n' '$(COMMAND)' '$(VERSION)'
	@grep -hE '^[a-z][a-z-]*:.*##' $(MAKEFILE_LIST) \
		| sed -E 's/:.*## /|/' \
		| awk -F'|' '{printf "  \033[1m%-11s\033[0m %s\n", $$1, $$2}'
	@printf '\n  prefix   %s\n  venv     %s\n\n' '$(PREFIX)' '$(VENV)'

build: check-brand $(STAMP) $(MANPAGE)  ## install the command and its man page (the one you want)
	@mkdir -p '$(BINDIR)' '$(MANDIR)'
	@ln -sf '$(VENV)/bin/$(COMMAND)' '$(BINDIR)/$(COMMAND)'
	@cp '$(MANPAGE)' '$(MANDIR)/$(COMMAND).1'
	@printf '\n  installed  %s\n  man page   %s\n' '$(BINDIR)/$(COMMAND)' '$(MANDIR)/$(COMMAND).1'
	@case ":$$PATH:" in \
		*":$(BINDIR):"*) ;; \
		*) printf '\n  note: %s is not on your PATH\n' '$(BINDIR)' ;; \
	esac
	@printf '\n  run `%s` to start, `man %s` for the rest\n\n' '$(COMMAND)' '$(COMMAND)'

install: build  ## alias for `build`

check-brand:  ## verify the two brand files agree
	@if [ '$(SCRIPT)' != '$(COMMAND)' ]; then \
		printf '\n  pyproject.toml installs the command as `%s`,\n' '$(SCRIPT)'; \
		printf '  but branding.COMMAND says `%s`.\n\n' '$(COMMAND)'; \
		printf '  These are the only two files that carry the name and they have to\n'; \
		printf '  match, or this would link a command that was never installed.\n'; \
		printf '  Fix [project.scripts] in pyproject.toml.\n\n'; \
		exit 1; \
	fi

uninstall:  ## remove the command and man page from PREFIX (leaves your data alone)
	@rm -f '$(BINDIR)/$(COMMAND)' '$(MANDIR)/$(COMMAND).1'
	@printf '  removed from %s — your runs are untouched in ~/.local/share/%s\n' '$(PREFIX)' '$(SLUG)'

man: $(MANPAGE)  ## render the man page without installing it
	@printf '  %s\n' '$(MANPAGE)'

test: $(STAMP)  ## run the test suite
	@$(PY) -m pytest -q

lint: $(STAMP)  ## check for unused imports and undefined names
	@$(PY) -m pyflakes '$(ROOT)/src' '$(ROOT)/tests' && echo '  clean'

clean:  ## drop caches and the rendered man page
	@rm -rf '$(BUILDDIR)' '$(ROOT)/.pytest_cache'
	@find '$(ROOT)/src' '$(ROOT)/tests' -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

distclean: clean  ## also delete the virtualenv
	@rm -rf '$(VENV)'

# --- the real work ---------------------------------------------------------

# Editable install, so day-to-day source edits need no reinstall. The stamp
# depends on pyproject.toml alone: that is the only file whose change requires
# one.
$(STAMP): $(ROOT)/pyproject.toml | $(PY)
	@cd '$(ROOT)' && if [ -n '$(UV)' ]; then \
		uv pip install --python '$(PY)' -e '$(SPEC)' -q; \
	else \
		'$(PY)' -m pip install --upgrade pip -q && '$(PY)' -m pip install -e '$(SPEC)' -q; \
	fi
	@touch '$@'

$(PY):
	@if [ -n '$(UV)' ]; then uv venv '$(VENV)' -q; else $(PYTHON) -m venv '$(VENV)'; fi

$(MANPAGE): $(ROOT)/man/app.1.in $(ROOT)/src/core/branding.py
	@mkdir -p '$(BUILDDIR)'
	@sed -e 's|@COMMAND_UPPER@|$(CMD_UPPER)|g' \
	     -e 's|@COMMAND@|$(COMMAND)|g' \
	     -e 's|@NAME@|$(APPNAME)|g' \
	     -e 's|@SLUG@|$(SLUG)|g' \
	     -e 's|@ENVPFX@|$(ENVPFX)|g' \
	     -e 's|@VERSION@|$(VERSION)|g' \
	     -e 's|@DESCRIPTION@|$(DESCRIPTION)|g' \
	     -e 's|@DATE@|$(TODAY)|g' \
	     '$<' > '$@'
