class MoodleAPIBreakageError(Exception):
    """ An error indicating a Moodle API structure change. """

    def __init__(self) -> None:
        super().__init__("Moodle data structure has changed. Contact project developers.")
