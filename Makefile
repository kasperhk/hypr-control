.PHONY: install test

install:
	./install.sh

test:
	python3 -m unittest discover -v
