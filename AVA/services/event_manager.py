class EventManager:

    def __init__(self, schedule):
        self.schedule = list(schedule)
        self.current_index = 0

    def get_current(self):
        if not self.schedule:
            return None

        return self.schedule[self.current_index]

    def get_previous(self):
        if self.current_index <= 0:
            return None

        return self.schedule[self.current_index - 1]

    def get_next(self):
        if self.current_index >= len(self.schedule) - 1:
            return None

        return self.schedule[self.current_index + 1]

    def next_event(self):
        if self.current_index < len(self.schedule) - 1:
            self.current_index += 1

        return self.get_current()

    def previous_event(self):
        if self.current_index > 0:
            self.current_index -= 1

        return self.get_current()

    def get_flow_context(self):
        return {
            "previous": self.get_previous(),
            "current": self.get_current(),
            "next": self.get_next()
        }