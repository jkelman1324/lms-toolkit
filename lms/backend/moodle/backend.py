# pylint: disable=abstract-method

import json
import logging
import re
import typing
import urllib.parse

import bs4
import edq.net.request
import requests

import lms.backend.moodle.errors
import lms.model.backend
import lms.model.constants
import lms.util.net

_logger = logging.getLogger(__name__)

ROLE_MAPPING: typing.Dict[str, lms.model.users.CourseRole] = {
    "guest": lms.model.users.CourseRole.OTHER,
    "student": lms.model.users.CourseRole.STUDENT,
    "non-editing teacher": lms.model.users.CourseRole.GRADER,
    "teacher": lms.model.users.CourseRole.ADMIN,
    "manager": lms.model.users.CourseRole.OWNER,
}

# Moodle shows 5000 users per page when asked to fetch all results.
RESULTS_PER_PAGE: int = 5000

class MoodleBackend(lms.model.backend.APIBackend):
    """ An API backend for the Moodle LMS. """

    def __init__(self,
            **kwargs: typing.Any) -> None:
        super().__init__(**kwargs)

        assert(self.config.backend_type == lms.model.constants.BackendType.MOODLE)

        if (self.config.auth_user is None):
            raise ValueError("Moodle backends require a username.")

        self.auth_user: str = self.config.auth_user
        """
        The user to authenticate with.
        This is set in config and compied for type checking.
        """

        if (self.config.auth_password is None):
            raise ValueError("Moodle backends require a password.")

        self.auth_password: str = self.config.auth_password.cleartext
        """
        The (cleartext) password to authenticate with.
        This is set in config and compied for type checking.
        """

        self._session_headers: typing.Union[typing.Dict[str, typing.Any], None] = None
        """ The headers (e.g., cookies) for our logged in Moodle session. """

    def _get_edit_mode_page(self, url: str, **kwargs: typing.Any) -> typing.Union[requests.Response, None]:
        """
        Tries to fetch the page at the given url with edit mode enabled.
        Returns the request response and a boolean indicating the status of edit mode.
        """

        try:
            response, _ = edq.net.request.make_get(url, headers = self.get_standard_headers(), **kwargs)
        except requests.exceptions.HTTPError:
            return None

        sesskey_match = re.search(r'"sesskey":"([^"]+)"', response.text)
        if (sesskey_match is None):
            raise lms.backend.moodle.errors.MoodleAPIBreakageError()

        sesskey = sesskey_match.group(1)

        document = bs4.BeautifulSoup(response.text, 'html.parser')

        element = document.select_one('input[name=setmode]')
        if (element is None):
            raise lms.backend.moodle.errors.MoodleAPIBreakageError()

        context_str = element.get('data-context')
        if (context_str is None):
            raise lms.backend.moodle.errors.MoodleAPIBreakageError()

        if (not isinstance(context_str, str)):
            raise lms.backend.moodle.errors.MoodleAPIBreakageError()

        context = int(context_str)

        params = {
            'sesskey': sesskey,
            'info': 'core_change_editmode',
        }

        data = [
            {
                'index': 0,
                'methodname': 'core_change_editmode',
                'args': {
                    'setmode': True,
                    'context': context,
                },
            }
        ]

        response, _ = edq.net.request.make_post(
            f"{self.server}/lib/ajax/service.php",
            additional_requests_options = {'params': params},
            data = json.dumps(data),
            headers = self.get_standard_headers(),
        )

        response, _ = edq.net.request.make_get(url, headers = self.get_standard_headers(), **kwargs)

        return response

    def reset_connection(self) -> None:
        self._session_headers = None

    def get_standard_headers(self, write: bool = False) -> typing.Dict[str, str]:
        headers = super().get_standard_headers(write)

        if (self._session_headers is not None):
            headers.update(self._session_headers)

        return headers

    def _parse_cookies(self, response: requests.Response) -> typing.Dict[str, typing.Any]:
        """
        Parse Moodle cookies.
        Return fake cookies when testing.
        """

        if (self.is_testing()):
            return {
                'moodlesession': 'testing-moodle-session',
                'moodleid1_': 'testing-moodle-id',
            }

        return lms.util.net.parse_cookies(response.headers.get('set-cookie', None))

    def _login(self, update_server: bool = True) -> None:
        """
        Try to login to the Moodle server.
        If `update_server` is true, then this may try to update the backend's server location if redirected by the Moodle server.
        """

        # Check if we are already logged in.
        if (self._session_headers is not None):
            return

        response, body = edq.net.request.make_get(self.server + '/login/index.php')
        cookies = self._parse_cookies(response)

        new_cookies = {
            'MoodleSession': cookies['moodlesession'],
        }
        text_cookies = '; '.join(['='.join(items) for items in new_cookies.items()])

        # Parse the login token from the page HTML.
        document = bs4.BeautifulSoup(body, 'html.parser')
        token = document.select('input[name="logintoken"]')[0]['value']

        headers = {
            'cookie': text_cookies,
            'host': urllib.parse.urlparse(self.server).netloc,
        }

        data = {
            'logintoken': token,
            'username': self.auth_user,
            'password': self.auth_password,
        }

        response, _ = edq.net.request.make_post(self.server + '/login/index.php',
                headers = headers, data = data,
                allow_redirects = False)

        # Check for a successful login.
        cookies = self._parse_cookies(response)
        if ('moodleid1_' in cookies):
            self._session_headers = {
                'cookie': response.headers.get('set-cookie', None),
                # Insert a header to identify the user.
                'edq-lms-moodle-user': self.auth_user,
            }

            return

        # Login Failed

        # The specified server/host needs to match exactly what the Moodle server wants it to be,
        # e.g., `127.0.0.1` does not work when the server wants the host to be `localhost`.
        # If these do not match, we will get a redirect here.
        # Use this redirect to discover the correct server.
        location = response.headers.get('location', None)
        if (update_server and (location is not None) and (not location.startswith(self.server))):
            parts = urllib.parse.urlparse(location)
            host = f"{parts.scheme}://{parts.netloc}"

            _logger.debug(("Mismatch in the client-specified server ('%s') and server-requested host ('%s')."
                    + " To avoid extra requests, update the server (e.g., `--server`) to match the host."),
                    self.server, host)

            # Update the server and try to login again (without updating the server again (to avoid loops)).
            self.server = host
            self._login(update_server = False)
            return

        raise ValueError(f"Could not log into Moodle server ({self.server}) with user '{self.auth_user}'. Is username/password correct?")

    def courses_list(self,
            **kwargs: typing.Any) -> typing.List[lms.model.courses.Course]:
        self._login()

        url = self.server + "/user/profile.php"
        response, _ = edq.net.request.make_get(url, headers = self.get_standard_headers())

        document = bs4.BeautifulSoup(response.text, 'html.parser')
        cards = document.select('div.card-body')

        node = None
        for card in cards:
            text = card.get_text()
            if (text.startswith("Course details")):
                node = card
                break

        if (node is None):
            return []

        links = node.select('a')

        courses = []
        for link in links:
            name = link.get_text()

            href = link.get('href', None)
            if (href is None):
                continue

            id = str(href).rsplit("=", maxsplit = 1)[-1]

            courses.append(lms.model.courses.Course(
                id = id,
                name = name,
            ))

        return sorted(courses)

    def courses_users_list(self,
            course_id: str,
            **kwargs: typing.Any) -> typing.List[lms.model.users.CourseUser]:
        self._login()

        url = f"{self.server}/user/index.php?id={course_id}&perpage={RESULTS_PER_PAGE}"
        response, _ = edq.net.request.make_get(url, headers = self.get_standard_headers())

        document = bs4.BeautifulSoup(response.text, 'html.parser')

        headers = document.select('table#participants thead tr th')
        # { course_user_attribute (e.g. name): column class, ... }
        classes = {}
        for header in headers:
            column_classes = header.get('class', None)
            if (column_classes is None):
                continue

            # Parse and store the column's class (e.g. "c0").
            # This class is referenced when storing corresponding course user data.
            if (isinstance(column_classes, str)):
                column_class = column_classes
            else:
                if ('header' in column_classes):
                    column_classes.remove('header')

                if (len(column_classes) != 1):
                    continue

                column_class = column_classes[0]

            elements = header.select('div.commands a')
            for element in elements:
                attribute = element.get('data-column', None)
                if (attribute is None):
                    continue

                classes[attribute] = column_class

        rows = document.select('table#participants tbody tr:not(.emptyrow)')

        users = []
        for row in rows:
            try:
                id = row.select_one('.cell input[type="checkbox"]').get('id', None).removeprefix('user')  # type: ignore[union-attr]
                name = row.select_one(f'.cell.{classes["fullname"]} a span').get('title', None).removeprefix('__EMPTY_NAME__ ')  # type: ignore[union-attr] # pylint: disable=line-too-long
                email = row.select_one(f'.cell.{classes["email"]}').get_text()  # type: ignore[union-attr]
                raw_role = row.select_one(f'.cell.{classes["roles"]} span a').get_text().strip().lower()  # type: ignore[union-attr]
            except AttributeError as ex:
                raise lms.backend.moodle.errors.MoodleAPIBreakageError() from ex

            # HACK(JK): Moodle does not allow the Guest role when loading test data, so we patch the guest role during testing.
            if (email == 'course-other@test.edulinq.org'):
                raw_role = "guest"

            users.append(lms.model.users.CourseUser(
                id = id,
                name = name,
                email = email,
                raw_role = raw_role,
                role = ROLE_MAPPING.get(raw_role, None),
            ))

        return users

    def courses_assignments_list(self,
            course_id: str,
            **kwargs: typing.Any) -> typing.List[lms.model.assignments.Assignment]:
        self._login()

        url = f"{self.server}/grade/report/grader/index.php?id={course_id}"

        # Attempt to enable edit mode on the gradebook page.
        response = self._get_edit_mode_page(url, enable_edit_mode = True)

        if (response is not None):
            return self._fetch_assignments_grader(response)

        return self._fetch_assignments_non_grader(course_id)

    def _fetch_assignments_grader(self, response: requests.Response) -> typing.List[lms.model.assignments.Assignment]:
        """
        Fetch assignment data for users with grader permissions.
        """

        assignments = []

        document = bs4.BeautifulSoup(response.text, 'html.parser')

        activities = document.select('table#user-grades th.item')
        for activity in activities:
            # Parse and store the column's class (e.g. "c0").
            target_class = None

            column_classes = activity.get('class', None)
            if (column_classes is None):
                raise lms.backend.moodle.errors.MoodleAPIBreakageError()

            for column_class in column_classes:
                if (re.search(r'^c\d+$', column_class) is not None):
                    target_class = column_class
                    break

            try:
                id = str(activity.get('data-itemid', None))
                name = str(activity.select_one('a.gradeitemheader').get_text())  # type: ignore[union-attr]

                points_possible_str = document.select_one(f'td.{target_class} input').get('max', None)  # type: ignore[union-attr]
                if (not isinstance(points_possible_str, str)):
                    points_possible = 0.0
                else:
                    points_possible = float(points_possible_str)
            except AttributeError as ex:
                raise lms.backend.moodle.errors.MoodleAPIBreakageError() from ex

            assignments.append(lms.model.assignments.Assignment(
                id = id,
                name = name,
                points_possible = points_possible,
            ))

        return assignments

    def _fetch_assignments_non_grader(self, course_id: str) -> typing.List[lms.model.assignments.Assignment]:
        """
        Fetch assignment data for users without grader permissions.
        """

        assignments = []

        url = f"{self.server}/grade/report/user/index.php?id={course_id}"
        response, _ = edq.net.request.make_get(url, headers = self.get_standard_headers())

        document = bs4.BeautifulSoup(response.text, 'html.parser')

        activities: typing.List[bs4.Tag] = list(document.find_all('tr[class]:not([class=""]):not(.spacer):not(.lastrow)'))
        for activity in activities:
            try:
                id = str(activity.select_one('th').get('id', None).split('_')[1])  # type: ignore[union-attr]
                name = str(activity.select_one('th a').get_text())  # type: ignore[union-attr]
                points_possible = float(activity.select_one('td.column-range').get_text().split('–')[1])  # type: ignore[union-attr]
            except AttributeError as ex:
                raise lms.backend.moodle.errors.MoodleAPIBreakageError() from ex

            assignments.append(lms.model.assignments.Assignment(
                id = id,
                name = name,
                points_possible = points_possible,
            ))

        return assignments
