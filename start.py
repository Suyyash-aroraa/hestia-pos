"""Start Hestia POS without storing an administrator password in source files."""
import getpass
import os
import sys


def configure_admin_password():
    if os.environ.get('HESTIA_ADMIN_PASSWORD', '').strip():
        return
    print('Hestia POS has no default admin password.')
    print('Enter the password to use for this run. It will not be saved to disk.')
    while True:
        password = getpass.getpass('Admin password: ')
        if not password.strip():
            print('A non-empty password is required.')
            continue
        if password != getpass.getpass('Confirm password: '):
            print('Passwords do not match. Try again.')
            continue
        os.environ['HESTIA_ADMIN_PASSWORD'] = password
        return


def main():
    configure_admin_password()
    if '--server' in sys.argv:
        from app import app
        import config
        from waitress import serve
        print(f'Hestia POS: http://127.0.0.1:{config.PORT}')
        serve(app, host=config.HOST_IP, port=config.PORT, threads=16, channel_timeout=600)
    else:
        from launcher import main as launch
        launch()


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print('\nStartup cancelled.')
        raise SystemExit(1)
