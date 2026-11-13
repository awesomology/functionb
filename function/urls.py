"""
URL configuration for function project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, re_path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve
from django.http import HttpResponseForbidden

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/accounts/', include('accounts.urls')),
]

if settings.DEBUG:
    # 1. Intercept the KYC folder specifically and block it
    urlpatterns += [
        re_path(r'^media/kyc_documents/(?P<path>.*)$', lambda request, path: HttpResponseForbidden("<style>body{margin:0}</style><main style='width:100vw;height:100vh;background:linear-gradient(to bottom, #203A43, #0F2027);display:flex;justify-content:center;align-items:center;'><h1 style='font-size:6em;color:#333;margin:0;'>Access Denied</h1></main>")),
    ]
    
    # 2. Allow other media files (like profile pictures) to be served normally (if you want)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)