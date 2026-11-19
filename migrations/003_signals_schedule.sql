-- At most one pending daily signals refresh, however many times the app restarts.
CREATE UNIQUE INDEX jobs_one_signals_refresh ON jobs (kind)
    WHERE kind = 'signals_refresh' AND status = 'queued';
