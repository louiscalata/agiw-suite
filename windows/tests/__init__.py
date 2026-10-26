"""Windows edition tests, a package so CI can run them from the repository root without clashing with the Mac
edition's same-named root tests (test_probes, test_server):

    python -m unittest discover -s windows/tests -t windows

From windows/ itself, ``python -m unittest discover -s tests`` keeps working. The root discovery
(``python -m unittest discover -p 'test_*.py'``) does not descend here, because windows/ is not a package.
"""
