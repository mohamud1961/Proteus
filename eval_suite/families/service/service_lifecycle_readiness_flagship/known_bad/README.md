# Known bad

Known-bad behaviors for this flagship:

1. **Wrong port**: Service binds to port 18924 instead of 18923. The
   independent probe connects to 18923 and gets connection refused.
   Must fail.

2. **Crash before second observation**: Service starts and responds to
   the first probe, then crashes before the 1-second survival window
   completes. Must fail.
