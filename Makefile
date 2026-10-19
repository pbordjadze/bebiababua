# Convenience wrappers; tools/babua is the real interface.
.PHONY: check preview image deploy test

check:
	tools/babua check
preview:
	tools/babua preview
image:
	tools/babua image
deploy:
	tools/babua deploy
test:
	python3 -m unittest discover -s tests -v
