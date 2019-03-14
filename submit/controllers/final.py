"""
Provides the final preview and confirmation step.
"""

from typing import Tuple, Dict, Any

from werkzeug import MultiDict
from werkzeug.exceptions import InternalServerError, BadRequest
from flask import url_for
from wtforms import BooleanField
from wtforms.validators import InputRequired

from arxiv import status
from arxiv.forms import csrf
from arxiv.base import logging
from arxiv.users.domain import Session
from arxiv.submission import save, FinalizeSubmission
from arxiv.submission.exceptions import SaveError
from ..util import load_submission
from .util import validate_command, user_and_client_from_session

logger = logging.getLogger(__name__)  # pylint: disable=C0103

Response = Tuple[Dict[str, Any], int, Dict[str, Any]]  # pylint: disable=C0103


def finalize(method: str, params: MultiDict, session: Session,
             submission_id: int, **kwargs) -> Response:
    submitter, client = user_and_client_from_session(session)

    logger.debug(f'method: {method}, submission: {submission_id}. {params}')
    submission, submission_events = load_submission(submission_id)

    form = FinalizationForm(params)
    response_data = {
        'submission_id': submission_id,
        'form': form,
        'submission': submission
    }

    if method == 'POST':
        if form.validate():
            logger.debug('Form is valid, with data: %s', str(form.data))
            abandoned_all_hope = form.proceed.data
            if abandoned_all_hope:
                command = FinalizeSubmission(creator=submitter)
                if not validate_command(form, command, submission):
                    raise BadRequest(response_data)

                try:
                    # Create ConfirmPolicy event
                    submission, stack = save(  # pylint: disable=W0612
                        command,
                        submission_id=submission_id
                    )
                except SaveError as e:
                    logger.error('Could not save primary event')
                    raise InternalServerError(response_data) from e
            if params.get('action') in ['previous', 'save_exit', 'next']:
                return response_data, status.HTTP_303_SEE_OTHER, {}
        else:   # Form data were invalid.
            raise BadRequest(response_data)

    return response_data, status.HTTP_200_OK, {}


class FinalizationForm(csrf.CSRFForm):
    """Make sure the user is really really really ready to submit."""

    proceed = BooleanField(
        'This is the point of no return! Today is a good day to die.',
        [InputRequired('You cannot proceed without giving up all hope')]
    )