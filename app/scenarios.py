from datetime import datetime, timezone, timedelta
import secrets


class Scenarios:
    def __init__(self, settings, repo, telemetry, clock=None):
        self.settings, self.repo, self.telemetry = settings, repo, telemetry
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def status(self):
        now = self.clock()
        override = self.repo.get("scenario-override")
        if override and override["expires_at"] > now.timestamp():
            phase = override["phase"]
            release = override["release"]
            expires = override["expires_at"]
            source = "override"
        else:
            hour, minute = map(int, self.settings.daily_start.split(":"))
            start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now < start:
                start -= timedelta(days=1)
            end = start + timedelta(seconds=self.settings.window_seconds)
            phase = "regression" if start <= now < end else "healthy"
            release = f"batch-{'removed' if phase == 'regression' else 'restored'}-{start if phase == 'regression' else now:%Y%m%d}"
            expires, source = end.timestamp() if phase == "regression" else None, "daily"
        result = {"phase": phase, "scenario": "missing-batch-read", "release": release,
                  "expires_at": expires, "source": source, "service_version": self.settings.version}
        prior = self.repo.get("scenario-observed")
        if prior != release:
            self.repo.set("scenario-observed", release)
            self.telemetry.event("Simulated batch-read rollout" if phase == "regression" else "Simulated recovery",
                **{"event.name": "deployment" if phase == "regression" else "feature_flag",
                   "demo.simulated": True, "demo.scenario": result["scenario"],
                   "demo.release": release, "demo.phase": phase, "service.version": self.settings.version})
        return result

    def activate(self, phase, duration):
        if phase not in ("healthy", "regression", "recover") or isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 600:
            raise ValueError("phase healthy/regression/recover and integer duration_seconds 1..600 required")
        now = self.clock()
        self.repo.set("scenario-override", {"phase": "healthy" if phase == "recover" else phase,
            "expires_at": now.timestamp() + duration, "release": f"mock-{phase}-{now:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"})
        return self.status()
