"""Optional email/password access and self-declared public author profiles.

The immutable actor record remains the authorship anchor. Email and password
state are private instance data and never enter portable publication bundles.
"""
from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
import hashlib
import hmac
import os
from pathlib import Path
import re
import queue
import secrets
import smtplib
import ssl
import time
import threading
import uuid

from .accounts import Accounts, MAX_SESSIONS, SESSION_SECONDS, active_session, fingerprint, transaction
from .store import Problem, display_name as check_display_name, identifier, record, text
from .i18n import current, t, use

TABLES = {'auth_passwords', 'auth_email_challenges', 'auth_profiles'}
REGISTRATION_TABLES = {'auth_pending_registrations'}
PBKDF2_ITERATIONS = 600_000
CHALLENGE_SECONDS = 20 * 60
MAX_PENDING_REGISTRATIONS = 4096
MAX_PENDING_MAIL = 128
EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z")


def email_address(value):
    text(value, 254)
    value = value.strip()
    if len(value) > 254 or not value.isascii() or not EMAIL_RE.fullmatch(value) or '..' in value:
        raise Problem('请输入有效的邮箱地址')
    local, domain = value.rsplit('@', 1)
    labels = domain.split('.')
    if (len(local) > 64 or local.startswith('.') or local.endswith('.')
            or any(len(part) > 63 or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?', part)
                   for part in labels)):
        raise Problem('请输入有效的邮箱地址')
    return local + '@' + domain.lower()


def password_value(value):
    text(value, 1024)
    if len(value) < 12 or len(value.encode('utf-8')) > 1024:
        raise Problem('密码至少12个字符，最多1024字节')
    return value


def password_hash(value):
    value = password_value(value)
    salt = secrets.token_bytes(16)
    result = hashlib.pbkdf2_hmac('sha256', value.encode(), salt, PBKDF2_ITERATIONS)
    return 'pbkdf2_sha256$%d$%s$%s' % (PBKDF2_ITERATIONS, salt.hex(), result.hex())


def verify_password(value, encoded):
    if not isinstance(value, str) or len(value) > 1024:
        return False
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or int(rounds) != PBKDF2_ITERATIONS:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', value.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, UnicodeError):
        return False


MAIL_SUBJECTS = {'verify': 'Eksmu 邮箱验证', 'register': 'Eksmu 注册邮箱确认',
                 'reset': 'Eksmu 重设密码', 'notice': 'Eksmu 密码已重设',
                 'analysis': 'Eksmu AI 分析报告已生成'}


def mail_text(purpose: str, code: str, lang: str) -> tuple[str, str]:
    """Subject and body in the language of the request that asked for the mail."""
    if purpose == 'notice':
        body = t(lang, '你的账户密码已重设。若不是你本人操作，请联系站点维护者。')
    elif purpose == 'analysis':
        body = t(lang, '你请求的作品 AI 分析报告已生成：\n{code}\n\n报告公开显示在作品页，作者和读者都可以逐条接受或反驳。', code=code)
    else:
        body = t(lang, '你的单次代码：{code}\n有效期20分钟。若不是你本人操作，请忽略。', code=code)
    return t(lang, MAIL_SUBJECTS[purpose]), body


def smtp_sender_from_environment():
    """Return None until the operator supplies a complete private SMTP config."""
    keys = ('EKSMU_SMTP_HOST', 'EKSMU_SMTP_FROM', 'EKSMU_SMTP_USER', 'EKSMU_SMTP_PASSWORD_FILE')
    if not all(os.environ.get(key) for key in keys):
        return None
    host, sender, user, password_file = (os.environ[key] for key in keys)
    port = int(os.environ.get('EKSMU_SMTP_PORT', '465'))
    email_address(sender)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid SMTP port')

    def send(recipient, purpose, code):
        secret_path = Path(password_file)
        if (not secret_path.is_file() or secret_path.stat().st_mode & 0o077
                or secret_path.stat().st_uid != os.getuid()):
            raise OSError('SMTP credential file must be private')
        password = secret_path.read_text(encoding='utf-8').strip()
        if not password:
            raise OSError('SMTP credential is empty')
        message = EmailMessage()
        message['From'] = formataddr(('Eksmu', sender))
        message['To'] = recipient
        message['Date'] = formatdate(usegmt=True)
        message['Message-ID'] = make_msgid(domain=sender.rsplit('@', 1)[1])
        subject, body = mail_text(purpose, code, current())
        message['Subject'] = subject
        message.set_content(body)
        if port == 587:
            with smtplib.SMTP(host, port, timeout=10) as connection:
                connection.starttls(context=ssl.create_default_context())
                connection.login(user, password)
                connection.send_message(message)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=10, context=ssl.create_default_context()) as connection:
                connection.login(user, password)
                connection.send_message(message)

    return send


class MailDispatcher:
    """One bounded worker keeps public responses independent of SMTP latency."""
    def __init__(self, sender):
        self.sender = sender
        self.queue = queue.Queue(MAX_PENDING_MAIL)
        self.closed = threading.Event()
        self.failures = 0
        self.thread = threading.Thread(target=self._run, name='eksmu-mail', daemon=True)
        self.thread.start()

    def __call__(self, recipient, purpose, code):
        if self.closed.is_set():
            raise OSError('Mail delivery is stopped')
        try:
            self.queue.put_nowait((recipient, purpose, code, current()))
        except queue.Full:
            raise OSError('Mail queue is full') from None

    def _run(self):
        while not self.closed.is_set():
            try:
                item = self.queue.get(timeout=.1)
            except queue.Empty:
                continue
            *mail, lang = item
            try:
                with use(lang):
                    self.sender(*mail)
            except Exception:
                self.failures += 1  # Never log recipients, codes or credentials.
            finally:
                self.queue.task_done()

    def close(self):
        self.closed.set()
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except queue.Empty:
                break
        self.thread.join(timeout=.2)


class ModernAccounts:
    def __init__(self, store, sender=None, *, require_verified_email=False):
        self.store, self.db, self.sender = store, store.db, sender
        self.require_verified_email = require_verified_email
        self.legacy = Accounts(store)
        existing = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if existing & TABLES and not TABLES <= existing:
            raise Problem('邮箱账户存储不完整，请维护者恢复', 503)
        if TABLES <= existing and (not require_verified_email or REGISTRATION_TABLES <= existing):
            return
        with transaction(self.db):
            if not TABLES <= existing:
                self.db.execute('''CREATE TABLE auth_passwords(
                    actor TEXT PRIMARY KEY REFERENCES auth_accounts(actor), email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, verified INTEGER NOT NULL, version INTEGER NOT NULL)''')
                self.db.execute('''CREATE TABLE auth_email_challenges(
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL REFERENCES auth_accounts(actor),
                    token_hash TEXT UNIQUE NOT NULL, purpose TEXT NOT NULL, expires INTEGER NOT NULL,
                    used INTEGER NOT NULL)''')
                self.db.execute('''CREATE TABLE auth_profiles(
                    actor TEXT PRIMARY KEY REFERENCES records(id), display_name TEXT NOT NULL,
                    affiliation TEXT NOT NULL, version INTEGER NOT NULL)''')
            if require_verified_email and not REGISTRATION_TABLES <= existing:
                self.db.execute('''CREATE TABLE auth_pending_registrations(
                    email TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                    expires INTEGER NOT NULL)''')

    def _ready(self):
        self.legacy._ready()

    def _new_session(self, actor, label):
        text(label, 64)
        label = label.strip()
        generation = self.db.execute('SELECT generation FROM auth_accounts WHERE actor=?', (actor,)).fetchone()[0]
        now = int(time.time())
        count = self.db.execute('SELECT COUNT(*) FROM auth_sessions WHERE actor=? AND revoked=0 AND expires>?',
                                (actor, now)).fetchone()[0]
        if count >= MAX_SESSIONS:
            raise Problem('有效会话已达20个，请先退出其他设备', 409)
        raw = 'ois1_' + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')
        sid = str(uuid.uuid4())
        self.db.execute('INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?,0)',
                        (sid, actor, fingerprint(raw), generation, now, now + SESSION_SECONDS, label))
        return self.legacy._result(actor, sid), raw

    def _challenge(self, actor, purpose):
        now = int(time.time())
        row = self.db.execute('''SELECT MAX(expires) FROM auth_email_challenges
            WHERE actor=? AND purpose=? AND expires>? AND used=0''', (actor, purpose, now)).fetchone()
        if row[0] is not None and row[0] - now > CHALLENGE_SECONDS - 60:
            raise Problem('请稍后再请求新代码', 429)
        self.db.execute('UPDATE auth_email_challenges SET used=1 WHERE actor=? AND purpose=?', (actor, purpose))
        raw = 'eksmu_' + secrets.token_urlsafe(32)
        self.db.execute('INSERT INTO auth_email_challenges VALUES(?,?,?,?,?,0)',
                        (str(uuid.uuid4()), actor, fingerprint(raw), purpose, now + CHALLENGE_SECONDS))
        return raw

    def _consume(self, actor, purpose, raw):
        text(raw, 128)
        row = self.db.execute('''SELECT id FROM auth_email_challenges
            WHERE actor=? AND token_hash=? AND purpose=? AND used=0 AND expires>?''',
            (actor, fingerprint(raw), purpose, int(time.time()))).fetchone()
        if row is None:
            raise Problem('代码无效或已过期', 400)
        self.db.execute('UPDATE auth_email_challenges SET used=1 WHERE id=?', (row[0],))

    def _retire_recovery(self, actor, keep_session=None):
        """Verified email becomes the recovery route; invalidate old file keys."""
        new_hash = fingerprint(secrets.token_urlsafe(32))
        self.db.execute('UPDATE auth_accounts SET recovery_hash=?,generation=generation+1 WHERE actor=?',
                        (new_hash, actor))
        self.db.execute('INSERT INTO auth_history VALUES(?,?)', (new_hash, actor))
        if keep_session is None:
            self.db.execute('UPDATE auth_sessions SET revoked=1 WHERE actor=?', (actor,))
        else:
            self.db.execute('UPDATE auth_sessions SET revoked=1 WHERE actor=? AND id<>?', (actor, keep_session))
            generation = self.db.execute('SELECT generation FROM auth_accounts WHERE actor=?', (actor,)).fetchone()[0]
            self.db.execute('UPDATE auth_sessions SET generation=? WHERE id=?', (generation, keep_session))

    def register(self, alias, email, password, label):
        if self.require_verified_email:
            return self.request_registration(email), None
        check_display_name(alias, email if isinstance(email, str) else None)
        text(label, 64)
        email = email_address(email)
        check_display_name(alias, email)
        encoded = password_hash(password)
        with transaction(self.db):
            self._ready()
            if self.db.execute('SELECT 1 FROM auth_passwords WHERE email=?', (email,)).fetchone():
                raise Problem('此邮箱已有账户，请登录或找回密码', 409)
            result, raw = self._create_account(alias, email, encoded, label, verified=False)
        result.update({'email_verified': False, 'verification_available': self.sender is not None})
        result['verification_sent'] = False
        if self.sender is not None:
            try:
                self.request_code('verify', token=raw)
                result['verification_sent'] = True
            except Problem:
                pass  # Account remains usable; the owner can request another code.
        return result, raw

    def _create_account(self, alias, email, encoded, label, *, verified):
        cleaned_alias = check_display_name(alias, email)
        actor = record('actor', None, {'alias': cleaned_alias,
            'key_hash': fingerprint(secrets.token_urlsafe(32))})
        self.store._append([actor])
        aid = actor['id']
        recovery_hash = fingerprint(secrets.token_urlsafe(32))
        self.db.execute('INSERT INTO auth_accounts VALUES(?,?,1)', (aid, recovery_hash))
        self.db.execute('INSERT INTO auth_history VALUES(?,?)', (recovery_hash, aid))
        self.db.execute('INSERT INTO auth_passwords VALUES(?,?,?,?,1)', (aid, email, encoded, int(verified)))
        self.db.execute('INSERT INTO auth_profiles VALUES(?,?,?,1)', (aid, cleaned_alias, ''))
        return self._new_session(aid, label)

    def request_registration(self, email):
        if not self.require_verified_email:
            raise Problem('此实例不使用注册前邮箱确认', 404)
        email = email_address(email)
        if self.sender is None:
            raise Problem('本站尚未配置验证邮件，暂不能注册', 503)
        result = {'request_received': True, 'verification_required': True}
        code = None
        now = int(time.time())
        with transaction(self.db):
            self._ready()
            self.db.execute('DELETE FROM auth_pending_registrations WHERE expires<=?', (now,))
            if self.db.execute('SELECT COUNT(*) FROM auth_pending_registrations').fetchone()[0] >= MAX_PENDING_REGISTRATIONS:
                raise Problem('注册请求暂时已满，请稍后重试', 503)
            known = self.db.execute('SELECT 1 FROM auth_passwords WHERE email=?', (email,)).fetchone()
            pending = self.db.execute('SELECT expires FROM auth_pending_registrations WHERE email=?', (email,)).fetchone()
            if not known and (not pending or pending[0] - now <= CHALLENGE_SECONDS - 60):
                code = 'eksmu_' + secrets.token_urlsafe(32)
                self.db.execute('''INSERT INTO auth_pending_registrations VALUES(?,?,?)
                    ON CONFLICT(email) DO UPDATE SET token_hash=excluded.token_hash,expires=excluded.expires''',
                    (email, fingerprint(code), now + CHALLENGE_SECONDS))
        if code is not None:
            try:
                self.sender(email, 'register', code)
            except Exception:
                # Registration responses never identify mailbox ownership or a
                # queue/delivery error affecting just one mailbox.
                pass
        return result

    def confirm_registration(self, email, code, alias, password, label):
        if not self.require_verified_email:
            raise Problem('此实例不使用注册前邮箱确认', 404)
        email = email_address(email)
        text(code, 128)
        check_display_name(alias, email)
        text(label, 64)
        encoded = password_hash(password)
        with transaction(self.db):
            self._ready()
            row = self.db.execute('''SELECT email FROM auth_pending_registrations
                WHERE email=? AND token_hash=? AND expires>?''',
                (email, fingerprint(code), int(time.time()))).fetchone()
            if row is None or self.db.execute('SELECT 1 FROM auth_passwords WHERE email=?', (email,)).fetchone():
                raise Problem('代码无效或已过期')
            result, token = self._create_account(alias, email, encoded, label, verified=True)
            self.db.execute('DELETE FROM auth_pending_registrations WHERE email=?', (email,))
        result.update({'email_verified': True, 'verification_available': True})
        return result, token

    def login(self, email, password, label):
        email = email_address(email)
        password_value(password)
        with transaction(self.db):
            self._ready()
            row = self.db.execute('SELECT actor,password_hash,verified FROM auth_passwords WHERE email=?', (email,)).fetchone()
            if row is None:
                # Spend one slow hash on unknown accounts too.
                hashlib.pbkdf2_hmac('sha256', password.encode(), b'\0' * 16, PBKDF2_ITERATIONS)
                raise Problem('邮箱或密码不正确', 401)
            valid = verify_password(password, row[1])
            if not valid or (self.require_verified_email and not row[2]):
                raise Problem('邮箱或密码不正确', 401)
            return self._new_session(row[0], label)

    def attach(self, token, credential, email, password):
        email = email_address(email)
        encoded = password_hash(password)
        with transaction(self.db):
            self._ready()
            actor = active_session(self.db, token)[1]
            if self.db.execute('SELECT 1 FROM auth_passwords WHERE actor=?', (actor,)).fetchone():
                raise Problem('此账户已绑定邮箱', 409)
            if self.db.execute('SELECT 1 FROM auth_passwords WHERE email=?', (email,)).fetchone():
                raise Problem('此邮箱已有账户', 409)
            owner = self.legacy._credential(credential)[0]
            if owner != actor:
                raise Problem('恢复凭证不属于当前账户', 403)
            self.db.execute('INSERT INTO auth_passwords VALUES(?,?,?,0,1)', (actor, email, encoded))
            alias = self.legacy._actor(actor)['payload']['alias']
            self.db.execute('INSERT OR IGNORE INTO auth_profiles VALUES(?,?,?,1)', (actor, alias, ''))
        sent = False
        if self.sender is not None:
            try:
                self.request_code('verify', token=token)
                sent = True
            except Problem:
                pass
        return {'attached': True, 'email_verified': False,
                'verification_available': self.sender is not None, 'verification_sent': sent}

    def public_profiles(self):
        actors = self.store._actors() if hasattr(self.store, '_actors') else (r for r in self.store.rows() if r['type'] == 'actor')
        known = {row['id']: row['payload']['alias'] for row in actors}
        result = {actor: {'display_name': alias, 'affiliation': '', 'self_declared': True}
                  for actor, alias in known.items()}
        for actor, name, affiliation, _ in self.db.execute('SELECT * FROM auth_profiles'):
            if actor in result:
                result[actor] = {'display_name': name, 'affiliation': affiliation, 'self_declared': True}
        return result

    def profile(self, token):
        self._ready()
        actor = active_session(self.db, token)[1]
        value = self.public_profiles()[actor]
        row = self.db.execute('SELECT version FROM auth_profiles WHERE actor=?', (actor,)).fetchone()
        contact = self.db.execute('SELECT email,verified FROM auth_passwords WHERE actor=?', (actor,)).fetchone()
        return {'actor_id': actor, **value, 'version': row[0] if row else 0,
                'email': contact[0] if contact else None, 'email_verified': bool(contact[1]) if contact else False,
                'verification_available': self.sender is not None}

    def update_profile(self, token, display_name, affiliation, expected_version):
        name = check_display_name(display_name)
        text(affiliation, 160, empty=True)
        if type(expected_version) is not int or expected_version < 0:
            raise Problem('资料版本不正确')
        affiliation = affiliation.strip()
        with transaction(self.db):
            self._ready()
            actor = active_session(self.db, token)[1]
            contact = self.db.execute('SELECT email FROM auth_passwords WHERE actor=?', (actor,)).fetchone()
            check_display_name(name, contact[0] if contact else None)
            row = self.db.execute('SELECT version FROM auth_profiles WHERE actor=?', (actor,)).fetchone()
            version = row[0] if row else 0
            if version != expected_version:
                raise Problem('公开资料已被其他设备修改，请刷新后核对', 409)
            if row:
                self.db.execute('UPDATE auth_profiles SET display_name=?,affiliation=?,version=? WHERE actor=?',
                                (name, affiliation, version + 1, actor))
            else:
                self.db.execute('INSERT INTO auth_profiles VALUES(?,?,?,1)', (actor, name, affiliation))
        return self.profile(token)

    def request_code(self, purpose, *, token=None, email=None):
        if self.sender is None:
            raise Problem('此私有实例尚未配置邮件发送，邮件找回暂不可用', 503)
        if purpose not in {'verify', 'reset'}:
            raise Problem('未知代码用途')
        with transaction(self.db):
            self._ready()
            if purpose == 'verify':
                actor = active_session(self.db, token)[1]
                row = self.db.execute('SELECT email,verified FROM auth_passwords WHERE actor=?', (actor,)).fetchone()
                if not row:
                    raise Problem('请先绑定邮箱', 409)
                if row[1]:
                    return {'sent': False, 'already_verified': True}
                recipient = row[0]
            else:
                recipient = email_address(email)
                row = self.db.execute('SELECT actor,verified FROM auth_passwords WHERE email=?', (recipient,)).fetchone()
                if row is None or not row[1]:
                    return {'request_received': True}
                actor = row[0]
            try:
                code = self._challenge(actor, purpose)
            except Problem as exc:
                if purpose == 'reset' and exc.status == 429:
                    # The caller learns nothing about whether this mailbox exists.
                    return {'request_received': True}
                raise
        try:
            self.sender(recipient, purpose, code)
        except Exception:
            with transaction(self.db):
                self.db.execute('UPDATE auth_email_challenges SET used=1 WHERE token_hash=?', (fingerprint(code),))
            if purpose == 'reset':
                # An SMTP error for a real account must not identify it.
                return {'request_received': True}
            raise Problem('邮件暂时无法发送，请稍后重试', 503) from None
        return {'request_received': True}

    def verify_email(self, token, code):
        with transaction(self.db):
            self._ready()
            current = active_session(self.db, token)
            actor = current[1]
            self._consume(actor, 'verify', code)
            self.db.execute('UPDATE auth_passwords SET verified=1,version=version+1 WHERE actor=?', (actor,))
            self._retire_recovery(actor, current[0])
        return {'email_verified': True, 'other_sessions_revoked': True, 'old_recovery_revoked': True}

    def reset_password(self, email, code, new_password):
        email = email_address(email)
        encoded = password_hash(new_password)
        with transaction(self.db):
            self._ready()
            row = self.db.execute('SELECT actor,verified FROM auth_passwords WHERE email=?', (email,)).fetchone()
            if row is None or not row[1]:
                raise Problem('代码无效或已过期')
            actor = row[0]
            self._consume(actor, 'reset', code)
            self.db.execute('UPDATE auth_passwords SET password_hash=?,version=version+1 WHERE actor=?', (encoded, actor))
            self._retire_recovery(actor)
        if self.sender is not None:
            try:
                self.sender(email, 'notice', '')
            except Exception:
                pass  # The committed password reset must remain committed.
        return {'password_reset': True, 'sessions_revoked': True}

    def change_password(self, token, current_password, new_password):
        password_value(current_password)
        encoded = password_hash(new_password)
        with transaction(self.db):
            self._ready()
            session = active_session(self.db, token)
            actor = session[1]
            row = self.db.execute('SELECT password_hash,verified FROM auth_passwords WHERE actor=?', (actor,)).fetchone()
            if row is None or not verify_password(current_password, row[0]):
                raise Problem('当前密码不正确', 401)
            if verify_password(new_password, row[0]):
                raise Problem('新密码应与当前密码不同')
            self.db.execute('UPDATE auth_passwords SET password_hash=?,version=version+1 WHERE actor=?',
                            (encoded, actor))
            self.db.execute('UPDATE auth_email_challenges SET used=1 WHERE actor=?', (actor,))
            if row[1]:
                self._retire_recovery(actor, session[0])
            else:
                self.db.execute('UPDATE auth_accounts SET generation=generation+1 WHERE actor=?', (actor,))
                generation = self.db.execute('SELECT generation FROM auth_accounts WHERE actor=?', (actor,)).fetchone()[0]
                self.db.execute('UPDATE auth_sessions SET revoked=1 WHERE actor=? AND id<>?', (actor, session[0]))
                self.db.execute('UPDATE auth_sessions SET generation=? WHERE id=?', (generation, session[0]))
        return {'password_changed': True, 'other_sessions_revoked': True}
