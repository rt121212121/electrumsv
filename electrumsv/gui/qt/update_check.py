
from distutils.version import StrictVersion
import requests

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QProgressBar, QLabel, QDialogButtonBox

from electrumsv.app_state import app_state
from electrumsv.gui.qt.util import read_QIcon, read_qt_ui
from electrumsv.i18n import _
from electrumsv.logs import logs
from electrumsv.util import get_update_check_dates
from electrumsv.version import PACKAGE_VERSION


MSG_TITLE_CHECK = "Checking ElectrumSV.io for Updates"
MSG_BODY_CHECK = "Please wait.."

MSG_BODY_ERROR = ("The update check encountered an error.<br/><br/>"+
    "{error_message}.")
MSG_BODY_UPDATE_AVAILABLE = (
    "This version of ElectrumSV, <b>{this_version}</b>, is obsolete.<br/>"+
    "The latest version, <b>{next_version}</b>, was released on "+
    "<b>{next_version_date:%Y/%m/%d %I:%M%p}</b>."+
    "<br/><br/>"+
    "Please download it from "+
    "<a href='{download_uri}{next_version}/'>electrumsv.io downloads</a>.")
MSG_BODY_NO_UPDATE_AVAILABLE = ("The update check was successful.<br/><br/>"+
    "You are already using <b>{this_version}</b>, which is the latest version.")
MSG_BODY_UNRELEASED_AVAILABLE = ("The update check was successful.<br/><br/>"+
    "You are already using <b>{this_version}</b>, which is the more recent than the "+
    "last official version <b>{latest_version}</b>.")
MSG_BODY_UNSTABLE_AVAILABLE = ("<br/><br/>"+
    "<i>The latest unstable release {unstable_version} was released on "+
    "{unstable_date:%Y/%m/%d %I:%M%p}.</i>")

logger = logs.get_logger("update_check.ui")


class UpdateCheckDialog(QWidget):
    _timer = None

    def __init__(self):
        super().__init__()

        self.setWindowTitle('ElectrumSV - ' + _('Update Check'))
        self.setWindowIcon(read_QIcon("electrum-sv.png"))
        self.resize(600, 400)

        widget: QWidget = read_qt_ui("updater_widget.ui")

        layout: QVBoxLayout = widget.findChild(QVBoxLayout, "vertical_layout")
        self._title_label: QLabel = widget.findChild(QLabel, "title_label")
        self._title_label.setText(_(MSG_TITLE_CHECK))
        self._progressbar: QProgressBar = widget.findChild(QProgressBar)
        self._progressbar.setValue(1)
        self._message_label: QLabel = widget.findChild(QLabel, "message_label")
        self._message_label.setText(_(MSG_BODY_CHECK))
        self._buttonbar: QDialogButtonBox = widget.findChild(QDialogButtonBox)
        self._buttonbar.rejected.connect(self.close)

        self.setLayout(layout)

        self.show()

        # The timer is to update the progress bar while the request is blocking the thread.
        counter = 0
        def _on_timer_event():
            nonlocal counter
            counter += 1
            progress_fraction = counter / (10 * 10)
            self._set_progress(progress_fraction)

        self._timer = QTimer()
        self._timer.timeout.connect(_on_timer_event)
        self._timer.start(1000/10)

        app_state.app.update_check_signal.connect(self._on_update_result)
        app_state.app.update_check()

    def closeEvent(self, event):
        self._stop_updates()
        self._timer = None

        event.accept()

    def _stop_updates(self):
        self._timer.stop()
        try:
            app_state.app.update_check_signal.disconnect(self._on_update_result)
        except TypeError:
            # TypeError: 'method' object is not connected
            # This can be called twice, easier to catch the exception than store additional state.
            pass

    def _set_progress(self, ratio):
        self._progressbar.setValue(int(ratio * 500))

    def _set_title(self, text):
        self._title_label.setText(text)

    def _set_message(self, text):
        # rt12 -- If the trailing breaks are not added, the QLabel depending on random
        # circumstances may clip and not display text from the end of the message.
        self._message_label.setText(text+"<br/><br/>")

    def _on_update_result(self, success, result):
        if success:
            self._on_update_success(result)
        else:
            self._on_update_error(result)

    def _on_update_success(self, result):
        self._stop_updates()

        # Indicate success by filling in the progress bar.
        self._set_progress(1.0)

        # Handle the case where data was fetched and it is incorrect or lacking.
        if type(result) is not dict or 'stable' not in result or 'unstable' not in result:
            self._set_message(_("The information about the latest version is broken."))
            return

        # Handle the case where the stable release is newer than our build.
        release_date, current_date = get_update_check_dates(result["stable"]["date"])
        stable_version = result["stable"]["version"]
        message = ""
        if release_date > current_date:
            message = _(MSG_BODY_UPDATE_AVAILABLE).format(
                this_version = PACKAGE_VERSION,
                next_version = stable_version,
                next_version_date = release_date,
                download_uri='https://electrumsv.io/download/')
        # Handle the case where the we are newer than the latest stable release.
        elif StrictVersion(stable_version) < StrictVersion(PACKAGE_VERSION):
            message = _(MSG_BODY_UNRELEASED_AVAILABLE).format(
                this_version=PACKAGE_VERSION,
                latest_version=stable_version)
        # Handle the case where we are the latest stable release.
        else:
            message = _(MSG_BODY_NO_UPDATE_AVAILABLE).format(
                this_version=stable_version)

        # By default users ignore unstable releases.
        if not app_state.config.get('check_updates_ignore_unstable', True):
            # If we are stable.  We show later unstable releases.
            # If we are unstable.  We show later unstable releases.
            unstable_result = result["unstable"]
            release_date, current_date = get_update_check_dates(unstable_result["date"])
            if release_date > current_date:
                message += _(MSG_BODY_UNSTABLE_AVAILABLE).format(
                    unstable_version = unstable_result["version"],
                    unstable_date = release_date
                )
        self._set_message(message)

    def _on_update_error(self, exc_info):
        self._stop_updates()

        message_text = _("Try again later, or consider reporting the problem.")
        if exc_info[0] is requests.exceptions.Timeout:
            message_text = _("The request took too long.") +" "+ message_text
        elif exc_info[0] is requests.exceptions.ConnectionError:
            message_text = _("Unable to connect.") +" "+ message_text
        else:
            message_text = str(exc_info[1]) +" "+ message_text

        # Change the color of the progress bar to red to reflect error.
        style_sheet = ("QProgressBar::chunk {background: QLinearGradient( x1: 0, y1: 0, "+
            "x2: 1, y2: 0,stop: 0 #FF0350,stop: 0.4999 #FF0020,stop: 0.5 #FF0019,"+
            "stop: 1 #FF0000 );border-bottom-right-radius: 5px;"+
            "border-bottom-left-radius: 5px;border: .px solid black;}")
        self._progressBar.setStyleSheet(style_sheet)
        self._set_progress(1.0)

        self._set_message(MSG_BODY_ERROR.format(error_message=message_text))
