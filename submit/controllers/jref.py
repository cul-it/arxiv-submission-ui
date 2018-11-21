"""Controller for JREF submissions."""

from typing import Tuple, Dict, Any, Optional

from werkzeug import MultiDict
from werkzeug.exceptions import InternalServerError, NotFound

from flask import url_for, Markup
from wtforms.fields import TextField, TextAreaField, Field
from wtforms.validators import InputRequired, ValidationError, optional

from arxiv import status
from arxiv.base import logging, alerts
from arxiv.forms import csrf
from arxiv.users.domain import Session
import arxiv.submission as events

from ..util import load_submission
from . import util

logger = logging.getLogger(__name__)  # pylint: disable=C0103

Response = Tuple[Dict[str, Any], int, Dict[str, Any]]  # pylint: disable=C0103


class JREFForm(csrf.CSRFForm, util.FieldMixin, util.SubmissionMixin):
    """Set DOI and/or journal reference on a published submission."""

    doi = TextField('DOI', validators=[optional()],
                    description="Full DOI of the version of record.")
    journal_ref = TextField('Journal reference', validators=[optional()],
                            description=(
                                "See <a href='https://arxiv.org/help/jref'>"
                                "the arXiv help pages</a> for details."
                            ))
    report_num = TextField('Report number', validators=[optional()],
                           description=(
                               "See <a href='https://arxiv.org/help/jref'>"
                               "the arXiv help pages</a> for details."
                           ))

    def validate_doi(form: csrf.CSRFForm, field: Field) -> None:
        """Validate DOI input using core events."""
        if field.data == form.submission.metadata.doi:     # Nothing to do.
            return
        if field.data:
            form._validate_event(events.SetDOI, doi=field.data)

    def validate_journal_ref(form: csrf.CSRFForm, field: Field) -> None:
        """Validate journal reference input using core events."""
        if field.data == form.submission.metadata.journal_ref:
            return
        if field.data:
            form._validate_event(events.SetJournalReference,
                                 journal_ref=field.data)


def jref(method: str, params: MultiDict, session: Session,
         submission_id: int) -> Response:
    """Set journal reference metadata on a published submission."""
    submitter, client = util.user_and_client_from_session(session)
    logger.debug(f'method: {method}, submission: {submission_id}. {params}')

    # Will raise NotFound if there is no such submission.
    submission, submission_events = load_submission(submission_id)

    # The submission must be published for this to be a real JREF submission.
    if not submission.published:
        alerts.flash_failure(Markup("Submission must first be published. See "
                                    "<a href='https://arxiv.org/help/jref'>"
                                    "the arXiv help pages</a> for details."))
        status_url = url_for('ui.submission_status',
                             submission_id=submission_id)
        return {}, status.HTTP_303_SEE_OTHER, {'Location': status_url}

    # The form should be prepopulated based on the current state of the
    # submission.
    if method == 'GET':
        params = MultiDict({
            'doi': submission.metadata.doi,
            'journal_ref': submission.metadata.journal_ref,
            'report_num': submission.metadata.report_num,
        })

    form = JREFForm(params)
    form.submission = submission
    form.creator = submitter
    response_data = {'submission_id': submission_id, 'form': form}

    if method == 'POST':
        if not form.validate():
            logger.debug('Invalid form data; return bad request')
            return response_data, status.HTTP_400_BAD_REQUEST, {}

        logger.debug('Form is valid, with data: %s', str(form.data))

        # form.events get set by the SubmissionMixin during validation.
        # TODO: that's kind of indirect.
        if form.events:   # Metadata has changed.
            try:
                # Save the events created during form validation.
                submission, stack = events.save(*form.events,
                                                submission_id=submission_id)
            except events.exceptions.InvalidStack as e:
                logger.error('Could not add JREF: %s', str(e))
                form.errors     # Causes the form to initialize errors.
                form._errors['events'] = [
                    ie.message for ie in e.event_exceptions
                ]
                logger.debug('InvalidStack; return bad request')
                return response_data, status.HTTP_400_BAD_REQUEST, {}
            except events.exceptions.SaveError as e:
                logger.error('Could not save metadata event')
                raise InternalServerError(
                    'There was a problem saving this operation'
                ) from e

            # Success! Send user back to the submission page.
            alerts.flash_success("Journal reference updated")
            status_url = url_for('ui.submission_status',
                                 submission_id=submission_id)
            return {}, status.HTTP_303_SEE_OTHER, {'Location': status_url}
    logger.debug('Nothing to do, return 200')
    return response_data, status.HTTP_200_OK, {}