.PHONY: verify test pytest clean help

help:
	@echo "EEMCP common commands:"
	@echo "  make verify   - Run 8 preflight tests (no real instruments)"
	@echo "  make pytest   - Run pytest suite (8 tests migrated from verify_preflight.py)"
	@echo "  make test     - Alias for make verify (legacy)"
	@echo "  make clean    - Remove __pycache__/"

verify:
	python verify_preflight.py

pytest:
	pytest tests/ -v

test:
	@echo "(pytest suite also available via 'make pytest'; running verify_preflight.py now)"
	python verify_preflight.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleaned __pycache__/"
