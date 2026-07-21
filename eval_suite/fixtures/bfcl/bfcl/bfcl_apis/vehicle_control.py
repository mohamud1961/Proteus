class _Base:
    def _load_scenario(self, cfg, long_context=False):
        self.state = dict(cfg or {})
        self.events = []

    def ping(self, **kwargs):
        self.events.append(("ping", kwargs))
        self.state.update(kwargs)
        return {"ok": True}


class MessageAPI(_Base):
    pass


class TicketAPI(_Base):
    pass


class TradingBot(_Base):
    pass


class TravelAPI(_Base):
    pass


class VehicleControlAPI(_Base):
    pass
