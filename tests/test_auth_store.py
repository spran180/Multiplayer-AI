from backend.auth import AuthError, AuthStore


def test_registration_sessions_and_room_invites_are_persisted(tmp_path):
    auth = AuthStore(sqlite_path=str(tmp_path / "auth.sqlite3"))
    try:
        owner = auth.register(" Person@Example.com ", "correct horse battery", "Person")
        assert auth.authenticate("person@example.com", "correct horse battery") == owner
        assert auth.authenticate("person@example.com", "wrong password") is None

        session = auth.create_session(owner["id"])
        assert auth.get_session_user(session) == owner
        auth.revoke_session(session)
        assert auth.get_session_user(session) is None

        invite = auth.add_room_owner("room-1", owner["id"])
        guest = auth.register("guest@example.com", "another strong password", "Guest")
        assert auth.authorize_room("room-1", guest["id"], invite)
        assert not auth.authorize_room("room-1", "unknown-user")
        assert set(auth.room_ids_for_user(guest["id"])) == {"room-1"}
    finally:
        auth.close()


def test_registration_rejects_duplicate_email(tmp_path):
    auth = AuthStore(sqlite_path=str(tmp_path / "auth.sqlite3"))
    try:
        auth.register("person@example.com", "a sufficiently long password", "Person")
        try:
            auth.register("PERSON@example.com", "another sufficiently long password", "Other")
        except AuthError:
            pass
        else:
            raise AssertionError("duplicate email was accepted")
    finally:
        auth.close()


def test_auth_attempts_are_rate_limited(tmp_path):
    auth = AuthStore(sqlite_path=str(tmp_path / "auth.sqlite3"))
    try:
        assert auth.consume_auth_rate_limit("login", "person@example.com", "127.0.0.1",
                                            email_limit=2, ip_limit=10)
        assert auth.consume_auth_rate_limit("login", "person@example.com", "127.0.0.1",
                                            email_limit=2, ip_limit=10)
        assert not auth.consume_auth_rate_limit("login", "person@example.com", "127.0.0.1",
                                                email_limit=2, ip_limit=10)
    finally:
        auth.close()
