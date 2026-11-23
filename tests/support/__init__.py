"""Reusable simulated-device seams for the test suite (issue #36).

`adb` is the scripted transport runner; `frames` is the recorded-frame
snapshotter. Both implement the production protocol they replace, so tests pass
them to the real clients instead of mocking a client method ad hoc.
"""
