help:
	@echo "Targets:"
	@echo "  make check:    Run all tests"
	@echo "  make podcheck: Run podcheck on our docs"
	@echo "  make pyflakes: Run pyflakes on our source"
	@echo "  make pylint:   Run pylint on our source"
	@echo "  make tests:    Run our pytest unit tests"

PYFLAKES=pyflakes-3
pyflakes:
	$(PYFLAKES) fabs

PYLINT=pylint-3
pylint:
	$(PYLINT) --rcfile pylintrc fabs

podcheck:
	./doc/check-pod doc/

TESTS=test -v
tests:
	# Note: run a single file like so:
	#  make tests TESTS=test/test_basic.py
	# Run a single test function like so:
	#  make tests TESTS=test/test_basic.py::TestBasic::test_vars
	# Run db-using tests against a mysql db (WARNING: will delete all data!) like so:
	#  make tests TESTS='--db-url=mysql://user:pass@host/dbname'

	# Running all tests
	python3 -m pytest $(TESTS)

quickcheck: pyflakes podcheck

check: quickcheck pylint tests
