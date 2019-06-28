"""
Controllers for upload-related requests.

Things that still need to be done:

- Display error alerts from the file management service.
- Show warnings/errors for individual files in the table. We may need to
  extend the flashing mechanism to "flash" data to the next page (without
  displaying it as a notification to the user).

"""

from typing import Tuple, Dict, Any, Optional, List, Union, Mapping
import traceback
from werkzeug import MultiDict
from werkzeug.exceptions import InternalServerError, BadRequest, \
    MethodNotAllowed
from werkzeug.datastructures import FileStorage

from locale import strxfrm
from collections import OrderedDict

from wtforms import BooleanField, widgets, HiddenField, FileField
from wtforms.validators import DataRequired
from flask import url_for, Markup

from http import HTTPStatus as status
from arxiv.integration.api import exceptions
from arxiv.base import logging, alerts
from arxiv.forms import csrf
from arxiv.submission.domain.submission import SubmissionContent
from arxiv.submission import SetUploadPackage, UpdateUploadPackage, save, \
    Submission, User, Client, UnConfirmCompiledPreview
from arxiv.submission.exceptions import InvalidEvent, SaveError
from arxiv.users.domain import Session

from .util import validate_command, user_and_client_from_session
from ..util import load_submission, tidy_filesize
from ..services import FileManager
from ..domain import Upload, FileStatus

logger = logging.getLogger(__name__)

Response = Tuple[Dict[str, Any], int, Dict[str, Any]]  # pylint: disable=C0103

PLEASE_CONTACT_SUPPORT = Markup(
    'If you continue to experience problems, please contact'
    ' <a href="mailto:help@arxiv.org"> arXiv support</a>.'
)


def upload_files(method: str, params: MultiDict, session: Session,
                 submission_id: int, files: Optional[MultiDict] = None,
                 token: Optional[str] = None, **kwargs) -> Response:
    """
    Handle a file upload request.

    GET requests are treated as a request for information about the current
    state of the submission upload.

    POST requests are treated either as package upload or a request to replace
    a file. If a submission upload workspace does not already exist, the upload
    is treated as the former.

    Parameters
    ----------
    method : str
        ``GET`` or ``POST``
    params : :class:`MultiDict`
        The form data from the request.
    files : :class:`MultiDict`
        File data in the multipart request. Values should be
        :class:`FileStorage` instances.
    session : :class:`Session`
        The authenticated session for the request.
    submission_id : int
        The identifier of the submission for which the upload is being made.
    token : str
        The original (encrypted) auth token on the request. Used to perform
        subrequests to the file management service.

    Returns
    -------
    dict
        Response data, to render in template.
    int
        HTTP status code. This should be ``200`` or ``303``, unless something
        goes wrong.
    dict
        Extra headers to add/update on the response. This should include
        the `Location` header for use in the 303 redirect response, if
        applicable.

    """
    if files is None or token is None:
        logger.debug('Missing files or auth token')
        raise BadRequest("Missing files or auth token")

    submission, _ = load_submission(submission_id)

    rdata = {
        'submission_id': submission_id,
        'submission': submission,
        'form': UploadForm()
    }

    if method == 'GET':
        logger.debug('GET; load current upload state')
        return _get_upload(params, session, submission, rdata, token)

    # User is attempting an upload of some kind.
    elif method == 'POST':
        # Otherwise, treat this as an upload attempt.
        logger.debug('POST; user is uploading file(s)')
        return _post_upload(params, files, session, submission, rdata, token)
    raise MethodNotAllowed('Nope')


def delete_all(method: str, params: MultiDict, session: Session,
               submission_id: int, token: Optional[str] = None,
               **kwargs) -> Response:
    """
    Handle a request to delete all files in the workspace.

    Parameters
    ----------
    method : str
        ``GET`` or ``POST``
    params : :class:`MultiDict`
        The query or form data from the request.
    session : :class:`Session`
        The authenticated session for the request.
    submission_id : int
        The identifier of the submission for which the deletion is being made.
    token : str
        The original (encrypted) auth token on the request. Used to perform
        subrequests to the file management service.

    Returns
    -------
    dict
        Response data, to render in template.
    int
        HTTP status code. This should be ``200`` or ``303``, unless something
        goes wrong.
    dict
        Extra headers to add/update on the response. This should include
        the `Location` header for use in the 303 redirect response, if
        applicable.

    """
    if token is None:
        logger.debug('Missing auth token')
        raise BadRequest('Missing auth token')

    fm = FileManager.current_session()
    submission, submission_events = load_submission(submission_id)
    upload_id = submission.source_content.identifier
    submitter, client = user_and_client_from_session(session)

    rdata = {'submission': submission, 'submission_id': submission_id}

    if method == 'GET':
        form = DeleteAllFilesForm()
        rdata.update({'form': form})
        return rdata, status.OK, {}
    elif method == 'POST':
        form = DeleteAllFilesForm(params)
        rdata.update({'form': form})

        if not (form.validate() and form.confirmed.data):
            logger.debug('Invalid form data')
            raise BadRequest(rdata)

        try:
            stat = fm.delete_all(upload_id, token)
        except exceptions.RequestForbidden as e:
            alerts.flash_failure(Markup(
                'There was a problem authorizing your request. Please try'
                f' again. {PLEASE_CONTACT_SUPPORT}'
            ))
            logger.error('Encountered RequestForbidden: %s', e)
        except exceptions.BadRequest as e:
            alerts.flash_warning(Markup(
                'Something odd happened when processing your request.'
                f'{PLEASE_CONTACT_SUPPORT}'
            ))
            logger.error('Encountered BadRequest: %s', e)
        except exceptions.RequestFailed as e:
            alerts.flash_failure(Markup(
                'There was a problem carrying out your request. Please try'
                f' again. {PLEASE_CONTACT_SUPPORT}'
            ))
            logger.error('Encountered RequestFailed: %s', e)

        command = UpdateUploadPackage(creator=submitter, client=client,
                                      checksum=stat.checksum,
                                      uncompressed_size=stat.size,
                                      source_format=stat.source_format)
        commands = [command]
        if submission.submitter_compiled_preview:
            commands.append(UnConfirmCompiledPreview(creator=submitter))
        if not validate_command(form, command, submission):
            logger.debug('Command validation failed')
            raise BadRequest(rdata)

        try:
            submission, _ = save(*commands, submission_id=submission_id)
        except SaveError:
            alerts.flash_failure(Markup(
                'There was a problem carrying out your request. Please try'
                f' again. {PLEASE_CONTACT_SUPPORT}'
            ))

        redirect = url_for('ui.file_upload', submission_id=submission_id)
        return {}, status.SEE_OTHER, {'Location': redirect}
    raise MethodNotAllowed('Method not supported')


def delete(method: str, params: MultiDict, session: Session,
           submission_id: int, token: Optional[str] = None,
           **kwargs) -> Response:
    """
    Handle a request to delete a file.

    The file will only be deleted if a POST request is made that also contains
    the ``confirmed`` parameter.

    The process can be initiated with a GET request that contains the
    ``path`` (key) for the file to be deleted. For example, a button on
    the upload interface may link to the deletion route with the file path
    as a query parameter. This will generate a deletion confirmation form,
    which can be POSTed to complete the action.

    Parameters
    ----------
    method : str
        ``GET`` or ``POST``
    params : :class:`MultiDict`
        The query or form data from the request.
    session : :class:`Session`
        The authenticated session for the request.
    submission_id : int
        The identifier of the submission for which the deletion is being made.
    token : str
        The original (encrypted) auth token on the request. Used to perform
        subrequests to the file management service.

    Returns
    -------
    dict
        Response data, to render in template.
    int
        HTTP status code. This should be ``200`` or ``303``, unless something
        goes wrong.
    dict
        Extra headers to add/update on the response. This should include
        the `Location` header for use in the 303 redirect response, if
        applicable.

    """
    if token is None:
        logger.debug('Missing auth token')
        raise BadRequest('Missing auth token')

    fm = FileManager.current_session()
    submission, submission_events = load_submission(submission_id)
    upload_id = submission.source_content.identifier
    submitter, client = user_and_client_from_session(session)

    rdata = {'submission': submission, 'submission_id': submission_id}

    if method == 'GET':
        # The only thing that we want to get from the request params on a GET
        # request is the file path. This way there is no way for a GET request
        # to trigger actual deletion. The user must explicitly indicate via
        # a valid POST that the file should in fact be deleted.
        params = MultiDict({'file_path': params['path']})

    form = DeleteFileForm(params)
    rdata.update({'form': form})

    if method == 'POST':
        if not (form.validate() and form.confirmed.data):
            logger.debug('Invalid form data')
            raise BadRequest(rdata)

        stat: Optional[Upload] = None
        try:
            file_path = form.file_path.data
            stat = fm.delete_file(upload_id, file_path, token)
            alerts.flash_success(
                f'File <code>{form.file_path.data}</code> was deleted'
                ' successfully', title='Deleted file successfully',
                safe=True
            )
        except exceptions.RequestForbidden:
            alerts.flash_failure(Markup(
                'There was a problem authorizing your request. Please try'
                f' again. {PLEASE_CONTACT_SUPPORT}'
            ))
        except exceptions.BadRequest:
            alerts.flash_warning(Markup(
                'Something odd happened when processing your request.'
                f'{PLEASE_CONTACT_SUPPORT}'
            ))
        except exceptions.RequestFailed:
            alerts.flash_failure(Markup(
                'There was a problem carrying out your request. Please try'
                f' again. {PLEASE_CONTACT_SUPPORT}'
            ))

        if stat is not None:
            command = UpdateUploadPackage(creator=submitter,
                                          checksum=stat.checksum,
                                          uncompressed_size=stat.size,
                                          source_format=stat.source_format)
            if not validate_command(form, command, submission):
                logger.debug('Command validation failed')
                raise BadRequest(rdata)
            commands = [command]
            if submission.submitter_compiled_preview:
                commands.append(UnConfirmCompiledPreview(creator=submitter))
            try:
                submission, _ = save(*commands, submission_id=submission_id)
            except SaveError:
                alerts.flash_failure(Markup(
                    'There was a problem carrying out your request. Please try'
                    f' again. {PLEASE_CONTACT_SUPPORT}'
                ))
        redirect = url_for('ui.file_upload', submission_id=submission_id)
        return {}, status.SEE_OTHER, {'Location': redirect}
    return rdata, status.OK, {}


class UploadForm(csrf.CSRFForm):
    """Form for uploading files."""

    file = FileField('Choose a file...', validators=[DataRequired()])
    ancillary = BooleanField('Ancillary')


class DeleteFileForm(csrf.CSRFForm):
    """Form for deleting individual files."""

    file_path = HiddenField('File', validators=[DataRequired()])
    confirmed = BooleanField('Confirmed', validators=[DataRequired()])


class DeleteAllFilesForm(csrf.CSRFForm):
    """Form for deleting all files in the workspace."""

    confirmed = BooleanField('Confirmed', validators=[DataRequired()])


def _update(form: UploadForm, submission: Submission, stat: Upload,
            submitter: User, client: Optional[Client] = None,
            rdata: Dict[str, Any] = {}) -> Submission:
    """
    Update the :class:`.Submission` after an upload-related action.

    The submission is linked to the upload workspace via the
    :attr:`Submission.source_content` attribute. This is set using a
    :class:`SetUploadPackage` command. If the workspace identifier changes
    (e.g. on first upload), we want to execute :class:`SetUploadPackage` to
    make the association.

    Parameters
    ----------
    submission : :class:`Submission`
    _status : :class:`Upload`
    submitter : :class:`User`
    client : :class:`Client` or None

    """
    existing_upload = getattr(submission.source_content, 'identifier', None)

    commands = []
    if existing_upload == stat.identifier:
        command = UpdateUploadPackage(creator=submitter, client=client,
                                      checksum=stat.checksum,
                                      uncompressed_size=stat.size,
                                      compressed_size=stat.compressed_size,
                                      source_format=stat.source_format)
        commands.append(command)
        if submission.submitter_compiled_preview:
            commands.append(UnConfirmCompiledPreview(creator=submitter))
    else:
        command = SetUploadPackage(creator=submitter, client=client,
                                   identifier=stat.identifier,
                                   checksum=stat.checksum,
                                   compressed_size=stat.compressed_size,
                                   uncompressed_size=stat.size,
                                   source_format=stat.source_format)
        commands.append(command)

    if not validate_command(form, command, submission):
        logger.debug('Command validation failed')
        raise BadRequest(rdata)

    try:
        submission, _ = save(*commands, submission_id=submission.submission_id)
    except SaveError:
        alerts.flash_failure(Markup(
            'There was a problem carrying out your request. Please try'
            f' again. {PLEASE_CONTACT_SUPPORT}'
        ))
    rdata['submission'] = submission
    return submission


def _get_upload(params: MultiDict, session: Session, submission: Submission,
                rdata: Dict[str, Any], token) -> Response:
    """
    Get the current state of the upload workspace, and prepare a response.

    Parameters
    ----------
    params : :class:`MultiDict`
        The query parameters from the request.
    session : :class:`Session`
        The authenticated session for the request.
    submission : :class:`Submission`
        The submission for which to retrieve upload workspace information.

    Returns
    -------
    dict
        Response data, to render in template.
    int
        HTTP status code.
    dict
        Extra headers to add/update on the response.

    """
    rdata.update({'status': None, 'form': UploadForm()})

    if submission.source_content is None:
        # Nothing to show; should generate a blank-slate upload screen.
        return rdata, status.OK, {}

    fm = FileManager.current_session()

    upload_id = submission.source_content.identifier
    status_data = alerts.get_hidden_alerts('_status')
    if type(status_data) is dict and status_data['identifier'] == upload_id:
        stat = Upload.from_dict(status_data)
    else:
        try:
            stat = fm.get_upload_status(upload_id, token)
        except exceptions.RequestFailed as e:
            # TODO: handle specific failure cases.
            logger.debug('Failed to get upload status: %s', e)
            logger.error(traceback.format_exc())
            raise InternalServerError(rdata) from e
    rdata.update({'status': stat})
    if stat:
        rdata.update({'immediate_notifications': _get_notifications(stat)})
    return rdata, status.OK, {}


def _new_upload(params: MultiDict, pointer: FileStorage, session: Session,
                submission: Submission, rdata: Dict[str, Any], token: str) \
        -> Response:
    """
    Handle a POST request with a new upload package.

    This occurs in the case that there is not already an upload workspace
    associated with the submission. See the :attr:`Submission.source_content`
    attribute, which is set using :class:`SetUploadPackage`.

    Parameters
    ----------
    params : :class:`MultiDict`
        The form data from the request.
    pointer : :class:`FileStorage`
        The file upload stream.
    session : :class:`Session`
        The authenticated session for the request.
    submission : :class:`Submission`
        The submission for which the upload is being made.

    Returns
    -------
    dict
        Response data, to render in template.
    int
        HTTP status code. This should be ``303``, unless something goes wrong.
    dict
        Extra headers to add/update on the response. This should include
        the `Location` header for use in the 303 redirect response.

    """
    submitter, client = user_and_client_from_session(session)
    fm = FileManager.current_session()

    # Using a form object provides some extra assurance that this is a legit
    # request; provides CSRF goodies.
    params['file'] = pointer
    form = UploadForm(params)
    rdata.update({'form': form})

    if not form.validate():
        logger.debug('Invalid form data')
        raise BadRequest(rdata)

    try:
        stat = fm.upload_package(pointer, token)
    except exceptions.RequestFailed as e:
        alerts.flash_failure(Markup(
            'There was a problem carrying out your request. Please try'
            f' again. {PLEASE_CONTACT_SUPPORT}'
        ))
        logger.debug('Failed to upload package: %s', e)
        logger.error(traceback.format_exc())
        raise InternalServerError(rdata) from e

    submission = _update(form, submission, stat, submitter, client, rdata)
    converted_size = tidy_filesize(stat.size)
    if stat.status is Upload.Status.READY:
        alerts.flash_success(
            f'Unpacked {stat.file_count} files. Total submission'
            f' package size is {converted_size}',
            title='Upload successful'
        )
    elif stat.status is Upload.Status.READY_WITH_WARNINGS:
        alerts.flash_warning(
            f'Unpacked {stat.file_count} files. Total submission'
            f' package size is {converted_size}. See below for warnings.',
            title='Upload complete, with warnings'
        )
    elif stat.status is Upload.Status.ERRORS:
        alerts.flash_warning(
            f'Unpacked {stat.file_count} files. Total submission'
            f' package size is {converted_size}. See below for errors.',
            title='Upload complete, with errors'
        )
    alerts.flash_hidden(stat.to_dict(), '_status')

    loc = url_for('ui.file_upload', submission_id=submission.submission_id)
    return {}, status.SEE_OTHER, {'Location': loc}


def _new_file(params: MultiDict, pointer: FileStorage, session: Session,
              submission: Submission, rdata: Dict[str, Any], token: str) \
        -> Response:
    """
    Handle a POST request with a new file to add to an existing upload package.

    This occurs in the case that there is already an upload workspace
    associated with the submission. See the :attr:`Submission.source_content`
    attribute, which is set using :class:`SetUploadPackage`.

    Parameters
    ----------
    params : :class:`MultiDict`
        The form data from the request.
    pointer : :class:`FileStorage`
        The file upload stream.
    session : :class:`Session`
        The authenticated session for the request.
    submission : :class:`Submission`
        The submission for which the upload is being made.

    Returns
    -------
    dict
        Response data, to render in template.
    int
        HTTP status code. This should be ``303``, unless something goes wrong.
    dict
        Extra headers to add/update on the response. This should include
        the `Location` header for use in the 303 redirect response.

    """
    submitter, client = user_and_client_from_session(session)
    fm = FileManager.current_session()
    upload_id = submission.source_content.identifier

    # Using a form object provides some extra assurance that this is a legit
    # request; provides CSRF goodies.
    params['file'] = pointer
    form = UploadForm(params)
    rdata['form'] = form

    if not form.validate():
        logger.error('Invalid upload form: %s', form.errors)

        alerts.flash_failure("Something went wrong. Please try again.",
                             title="Whoops")
        # redirect = url_for('ui.file_upload',
        #                    submission_id=submission.submission_id)
        # return {}, status.SEE_OTHER, {'Location': redirect}
        logger.debug('Invalid form data')
        raise BadRequest(rdata)
    ancillary: bool = form.ancillary.data

    try:
        stat = fm.add_file(upload_id, pointer, token, ancillary=ancillary)
    except exceptions.RequestFailed as e:
        try:
            e_data = e.response.json()
        except Exception:
            e_data = None
        if e_data is not None and 'reason' in e_data:
            alerts.flash_failure(Markup(
                'There was a problem carrying out your request:'
                f' {e_data["reason"]}. {PLEASE_CONTACT_SUPPORT}'
            ))
            raise BadRequest(rdata)
        alerts.flash_failure(Markup(
            'There was a problem carrying out your request. Please try'
            f' again. {PLEASE_CONTACT_SUPPORT}'
        ))
        logger.debug('Failed to add file: %s', )
        logger.error(traceback.format_exc())
        raise InternalServerError(rdata) from e

    submission = _update(form, submission, stat, submitter, client, rdata)
    converted_size = tidy_filesize(stat.size)
    if stat.status is Upload.Status.READY:
        alerts.flash_success(
            f'Uploaded {pointer.filename} successfully. Total submission'
            f' package size is {converted_size}',
            title='Upload successful'
        )
    elif stat.status is Upload.Status.READY_WITH_WARNINGS:
        alerts.flash_warning(
            f'Uploaded {pointer.filename} successfully. Total submission'
            f' package size is {converted_size}. See below for warnings.',
            title='Upload complete, with warnings'
        )
    elif stat.status is Upload.Status.ERRORS:
        alerts.flash_warning(
            f'Uploaded {pointer.filename} successfully. Total submission'
            f' package size is {converted_size}. See below for errors.',
            title='Upload complete, with errors'
        )
    status_data = stat.to_dict()
    alerts.flash_hidden(status_data, '_status')
    loc = url_for('ui.file_upload', submission_id=submission.submission_id)
    return {}, status.SEE_OTHER, {'Location': loc}


def _post_upload(params: MultiDict, files: MultiDict, session: Session,
                 submission: Submission, rdata: Dict[str, Any], token: str) \
        -> Response:
    """
    Compose POST request handling for the file upload endpoint.

    See the :attr:`Submission.source_content` attribute, which is set using
    :class:`SetUploadPackage`.

    Parameters
    ----------
    params : :class:`MultiDict`
        The form data from the request.
    files : :class:`MultiDict`
        File data in the multipart request. Values should be
        :class:`FileStorage` instances.
    session : :class:`Session`
        The authenticated session for the request.
    submission : :class:`Submission`
        The submission for which the request is being made.

    Returns
    -------
    dict
        Response data, to render in template.
    int
        HTTP status code. This should be ``303``, unless something goes wrong.
    dict
        Extra headers to add/update on the response. This should include
        the `Location` header for use in the 303 redirect response.

    """
    try:    # Make sure that we have a file to work with.
        pointer = files['file']
    except KeyError:   # User is going back, saving/exiting, or next step.
        pointer = None

    if not pointer:
        # Don't flash a message if the user is just trying to go back to the
        # previous page.
        logger.debug('No files on request')
        action = params.get('action', None)
        if action:
            logger.debug('User is navigating away from upload UI')
            return {}, status.SEE_OTHER, {}
    if submission.source_content is None:   # New upload package.
        logger.debug('New upload package')
        return _new_upload(params, pointer, session, submission, rdata, token)
    logger.debug('Adding additional files')
    return _new_file(params, pointer, session, submission, rdata, token)


def _get_notifications(stat: Upload) -> List[Dict[str, str]]:
    # TODO: these need wordsmithing.
    notifications = []
    if not stat.files:   # Nothing in the upload workspace.
        return notifications
    if stat.status is Upload.Status.ERRORS:
        notifications.append({
            'title': 'Unresolved errors',
            'severity': 'danger',
            'body': 'There are unresolved problems with your submission'
                    ' files. Please correct the errors below before'
                    ' proceeding.'
        })
    elif stat.status is Upload.Status.READY_WITH_WARNINGS:
        notifications.append({
            'title': 'Warnings',
            'severity': 'warning',
            'body': 'There is one or more unresolved warning in the file list.'
                    ' You may proceed with your submission, but please note'
                    ' that these issues may cause delays in processing'
                    ' and/or announcement.'
        })
    if stat.source_format is SubmissionContent.Format.UNKNOWN:
        notifications.append({
            'title': 'Unknown submission type',
            'severity': 'warning',
            'body': 'We could not determine the source type of your'
                    ' submission. Please check your files carefully. We may'
                    ' not be able to process your files.'
        })
    elif stat.source_format is SubmissionContent.Format.INVALID:
        notifications.append({
            'title': 'Unsupported submission type',
            'severity': 'danger',
            'body': 'It is likely that your submission content is not'
                    ' supported. Please check your files carefully. We may not'
                    ' be able to process your files.'
        })
    else:
        notifications.append({
            'title': f'Detected {stat.source_format.value.upper()}',
            'severity': 'success',
            'body': 'Your submission content is supported.'
        })
    return notifications


NestedFileTree = Mapping[str, Union[FileStatus, 'NestedFileTree']]


def group_files(files: List[FileStatus]) -> NestedFileTree:
    """
    Group a set of file status objects by directory structure.

    Parameters
    ----------
    list
        Elements are :class:`FileStatus` objects.

    Returns
    -------
    :class:`OrderedDict`
        Keys are strings. Values are either :class:`FileStatus` instances
        (leaves) or :class:`OrderedDict` (containing more :class:`FileStatus`
        and/or :class:`OrderedDict`, etc).

    """
    # First step is to organize by file tree.
    tree = {}
    for f in files:
        parts = f.path.split('/')
        if len(parts) == 1:
            tree[f.name] = f
        else:
            subtree = tree
            for part in parts[:-1]:
                if part not in subtree:
                    subtree[part] = {}
                subtree = subtree[part]
            subtree[parts[-1]] = f

    # Reorder subtrees for nice display.
    def _order(node: Union[dict, FileStatus]) -> OrderedDict:
        if type(node) is FileStatus:
            return node

        in_subtree: dict = node

        # split subtree into FileStatus and other
        filestats = [fs for key, fs in in_subtree.items()
                     if type(fs) is FileStatus]
        deeper_subtrees = [(key, st) for key, st in in_subtree.items()
                           if type(st) is not FileStatus]

        # add the files at this level before any subtrees
        ordered_subtree = OrderedDict()
        if filestats and filestats is not None:
            for fs in sorted(filestats, key=lambda fs: strxfrm(fs.path.casefold())):
                ordered_subtree[fs.path] = fs

        if deeper_subtrees:
            for key, deeper in sorted(deeper_subtrees,
                                      key=lambda tup: strxfrm(tup[0].casefold())):
                ordered_subtree[key] = _order(deeper)

        return ordered_subtree

    return _order(tree)
