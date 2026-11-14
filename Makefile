PYTHON ?= python
export PYTHONPATH := src

.PHONY: install test mock demo clean

install:
	$(PYTHON) -m pip install ".[dev]" --no-build-isolation

test:
	$(PYTHON) -m pytest

mock:
	$(PYTHON) -m metaharvest mock --config configs/mock_repo.yml

demo:
	$(PYTHON) -m metaharvest demo --workdir build/demo \
		--mock-config configs/mock_repo.yml \
		--harvest-config configs/harvest_demo.yml

clean:
	rm -rf build var
