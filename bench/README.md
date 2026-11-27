# Benchmark harness (Phase 1: measures only)

Nothing here changes application code, and none of it ships in the installer payload.

    python bench/run.py                   # all sections, several minutes (run sections one at a time on small machines)
    python bench/run.py store library     # selected sections
    BENCH_SCENARIOS=long1h BENCH_MERGE=1 python bench/run.py pipeline    # one pipeline scenario, merged into results
    python bench/report.py                # renders docs/PERF-BASELINE.md from results/baseline.json

Sections: env installer host nm reconnect store library misc pipeline cancel.
Pipeline tests use the real ffmpeg/ffprobe with a stand-in yt-dlp that copies a fixture (so network and
YouTube are NOT measured). Fixtures are generated into bench/.cache (a 3-minute and a 1-hour AAC file).
`bench/Measure-Windows.ps1` collects the Windows-only numbers (untested: written without a Windows PC).
