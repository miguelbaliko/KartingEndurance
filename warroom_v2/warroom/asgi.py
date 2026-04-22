import os
from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'warroom.settings')

django_asgi = get_asgi_application()

import dashboard.routing

application = ProtocolTypeRouter({
    'http': django_asgi,
    'websocket': AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(dashboard.routing.websocket_urlpatterns)
        )
    ),
})
