import json

from django import forms
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
import webauthn
from webauthn import options_to_json, base64url_to_bytes
from webauthn.helpers.structs import PublicKeyCredentialDescriptor, AuthenticationCredential
from base64 import b64decode

from u2flib_server import u2f


class SecondFactorForm(forms.Form):
    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user')
        self.request = kwargs.pop('request')
        self.appId = kwargs.pop('appId')
        return super(SecondFactorForm, self).__init__(*args, **kwargs)


class KeyResponseForm(SecondFactorForm):
    response = forms.CharField()

    def __init__(self, *args, **kwargs):
        super(KeyResponseForm, self).__init__(*args, **kwargs)
        if self.data:
            self.sign_request = self.request.session['u2f_sign_request']
        else:
            options = webauthn.generate_authentication_options(
                rp_id='localhost',
                allow_credentials=[
                    PublicKeyCredentialDescriptor(id=base64url_to_bytes(x.key_handle))
                    for x in self.user.u2f_keys.all()
                ]
            )
            options = options_to_json(options)
            options = json.loads(options)

            # should appid come from the u2f_key model?
            options['extensions'] = {
                'appid': 'https://localhost:8000'
            }
            options = {'publicKey': options}
            self.sign_request = options
            self.request.session['u2f_sign_request'] = options

    def validate_second_factor(self):
        response = self.cleaned_data['response']
        try:
            data = self.request.session['u2f_sign_request']['publicKey']
            json_data = json.loads(response)
            key = self.user.u2f_keys.get(key_handle=json_data['id'])
            expected_rp_id = data['rpId']
            # use app id as expected_rp_id if appid extension is provided
            # https://github.com/duo-labs/py_webauthn/issues/116#issuecomment-1010385763
            if json_data['clientExtensionResults'].get('appid', False):
                expected_rp_id = key.app_id
            verification = webauthn.verify_authentication_response(
                credential=AuthenticationCredential.parse_raw(response),
                expected_challenge=base64url_to_bytes(data['challenge']),
                expected_rp_id=expected_rp_id,
                expected_origin='https://localhost:8000',
                credential_public_key=base64url_to_bytes(key.public_key),
                credential_current_sign_count=0,
                require_user_verification=False,
            )
            # TODO: store login_counter and verify it's increasing
            key.last_used_at = timezone.now()
            key.save()
            del self.request.session['u2f_sign_request']
            return True
        except ValueError:
            self.add_error('__all__', 'U2F validation failed -- bad signature.')
        return False


class KeyRegistrationForm(SecondFactorForm):
    response = forms.CharField()


class BackupCodeForm(SecondFactorForm):
    INVALID_ERROR_MESSAGE = _("That is not a valid backup code.")

    code = forms.CharField(label=_("Code"), widget=forms.TextInput(attrs={'autocomplete': 'off'}))

    def validate_second_factor(self):
        count, _ = self.user.backup_codes.filter(code=self.cleaned_data['code']).delete()
        if count == 0:
            self.add_error('code', self.INVALID_ERROR_MESSAGE)
            return False
        elif count == 1:
            return True
        else:
            assert False, \
                "Impossible, there should never be more than one object with the same code."


class TOTPForm(SecondFactorForm):
    INVALID_ERROR_MESSAGE = _("That token is invalid.")

    token = forms.CharField(
        min_length=6,
        max_length=6,
        label=_("Token"),
        widget=forms.TextInput(attrs={'autocomplete': 'off'})
    )

    def validate_second_factor(self):
        for device in self.user.totp_devices.all():
            if device.validate_token(self.cleaned_data['token']):
                device.last_used_at = timezone.now()
                device.save()
                return True
        self.add_error('token', self.INVALID_ERROR_MESSAGE)
        return False
