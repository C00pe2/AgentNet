from app.main import app
print('app loaded with routes:')
for r in app.routes:
    if hasattr(r, 'path'):
        methods = getattr(r, 'methods', None)
        print(f'  {str(methods):<30} {r.path}')
