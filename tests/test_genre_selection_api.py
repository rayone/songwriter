from unittest.mock import patch

from songwriter.web.app import InterviewRequest, cowriter, interview


def test_interview_api_forwards_original_genre_and_template():
    request = InterviewRequest(
        brief='a journey', style='atmospheric synths', genre='Progressive House / Vocal Trance',
        template_id=7, reference_song={'genre': 'Pop'}, model='test-model',
    )
    with patch.object(cowriter, 'interview', return_value={'questions': []}) as call:
        interview(request)
    assert call.call_args.kwargs['genre'] == 'Progressive House / Vocal Trance'
    assert call.call_args.kwargs['template_id'] == 7
    assert call.call_args.kwargs['reference_song']['genre'] == 'Pop'
