from services.event_manager import EventManager


schedule = [
    {
        "event_time": "10:00",
        "title": "Welcome"
    },
    {
        "event_time": "10:05",
        "title": "Prayer"
    },
    {
        "event_time": "10:10",
        "title": "Welcome Speech"
    },
    {
        "event_time": "10:20",
        "title": "Dance Performance"
    }
]


manager = EventManager(schedule)

print("CURRENT:")
print(manager.get_current())

print("\nNEXT:")
print(manager.get_next())

print("\nPREVIOUS:")
print(manager.get_previous())

print("\nMoving to next event...")

manager.next_event()

print("\nCURRENT:")
print(manager.get_current())

print("\nNEXT:")
print(manager.get_next())

print("\nPREVIOUS:")
print(manager.get_previous())