class MoodleAPIBreakageError(Exception):
    """ An error indicating a Moodle API structure change. """

    def __init__(self, message):
        super().__init__(f"{message} Moodle data structure has changed. Contact project developers.")
