"""Credential-scrubber tests. Run with:
    pixi run python -m unittest discover -s tests
"""
import unittest

from skillsynapse.sanitizer import REDACTED, scrub


class TestScrubRedacts(unittest.TestCase):
    """Everything credential-shaped must come out as <REDACTED>."""

    def assert_gone(self, text: str, secret: str):
        out = scrub(text)
        self.assertNotIn(secret, out, f"secret survived: {out!r}")
        self.assertIn(REDACTED, out, f"nothing redacted: {out!r}")

    def test_mysql_attached_password(self):
        self.assert_gone("mysql -u root -pS3cretPw! mydb", "S3cretPw!")

    def test_mysqldump_attached_password(self):
        self.assert_gone("mysqldump -u app -phunter2 db > dump.sql", "hunter2")

    def test_password_flag_equals(self):
        self.assert_gone("prog --password=topsecret --verbose", "topsecret")

    def test_password_flag_space(self):
        self.assert_gone("prog --password 'top secret'", "top secret")

    def test_sshpass(self):
        self.assert_gone("sshpass -p MyPass123 ssh user@host", "MyPass123")

    def test_curl_basic_auth(self):
        out = scrub("curl -u admin:hunter2 https://api.example.com")
        self.assertNotIn("hunter2", out)
        self.assertIn("admin:", out)  # username survives

    def test_url_embedded_credentials(self):
        out = scrub("git clone https://bob:s3cret@github.com/x/y.git")
        self.assertNotIn("s3cret", out)
        self.assertIn("bob", out)
        self.assertIn("@github.com", out)

    def test_env_assignment(self):
        self.assert_gone("export DB_PASSWORD=changeme", "changeme")

    def test_env_assignment_quoted(self):
        self.assert_gone('MYSQL_ROOT_PASSWORD="p@ss w0rd"', "p@ss w0rd")

    def test_yaml_style_assignment(self):
        self.assert_gone("api_key: abcd1234efgh", "abcd1234efgh")

    def test_token_assignment(self):
        self.assert_gone("HF_TOKEN=hf_abcdefg123456", "hf_abcdefg123456")

    def test_authorization_bearer(self):
        self.assert_gone(
            "curl -H 'Authorization: Bearer abc.def.ghi' https://x", "abc.def.ghi"
        )

    def test_anthropic_key(self):
        self.assert_gone(
            "ANTHROPIC_API_KEY=sk-ant-api03-AbCdEf123456789", "sk-ant-api03"
        )

    def test_bare_known_token(self):
        # No key= prefix at all — prefix pattern alone must catch it.
        self.assert_gone(
            "the key was sk-ant-abcdefghijklmnop in the logs", "sk-ant-abcdefghijklmnop"
        )

    def test_aws_access_key(self):
        self.assert_gone("aws configure set AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7")

    def test_github_pat(self):
        self.assert_gone(
            "git push https://ghp_abcdefghij0123456789abcd@github.com/x/y",
            "ghp_abcdefghij0123456789abcd",
        )

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4"
        self.assert_gone(f"Set-Cookie: session={jwt}", "SflKxwRJSMeKKF2QT4")

    def test_prose_password_english(self):
        self.assert_gone("the password is hunter2 for the admin panel", "hunter2")

    def test_prose_password_chinese(self):
        self.assert_gone("生产库密码是 hunter2，用户 root", "hunter2")

    def test_prose_password_chinese_colon(self):
        self.assert_gone("密码：S3cret!、端口 3306", "S3cret!")

    def test_pem_block(self):
        pem = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAA\n"
            "-----END OPENSSH PRIVATE KEY-----"
        )
        self.assert_gone(f"cat > key <<EOF\n{pem}\nEOF", "b3BlbnNzaC1rZXktdjEA")


class TestScrubPreserves(unittest.TestCase):
    """Benign text and placeholders must survive untouched."""

    def assert_unchanged(self, text: str):
        self.assertEqual(scrub(text), text)

    def test_ssh_port_attached(self):
        self.assert_unchanged("ssh -p2222 user@host")

    def test_ssh_port_spaced(self):
        self.assert_unchanged("ssh -p 22 user@host uptime")

    def test_pwd_env_var(self):
        self.assert_unchanged("echo PWD=/srv/app/code")

    def test_author_field(self):
        self.assert_unchanged("git log --author=shuai --oneline")

    def test_placeholder_env_ref(self):
        self.assert_unchanged("export DB_PASSWORD=$DB_PASSWORD")

    def test_placeholder_braced(self):
        self.assert_unchanged('PASSWORD="${VAULT_PW}"')

    def test_placeholder_angle_bracket(self):
        self.assert_unchanged("mysql -u root --password=<your-password> db")

    def test_plain_prose(self):
        self.assert_unchanged("Rebuild the index then restart the service.")

    def test_url_without_credentials(self):
        self.assert_unchanged("git clone https://github.com/x/y.git")

    def test_none_and_empty(self):
        self.assertIsNone(scrub(None))
        self.assertEqual(scrub(""), "")


if __name__ == "__main__":
    unittest.main()
