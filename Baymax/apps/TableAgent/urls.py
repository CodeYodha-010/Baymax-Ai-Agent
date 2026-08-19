from django.urls import path
from . import views

urlpatterns = [
    path('', views.UploadFileView.as_view(), name='upload'),
    path('ask/', views.AskQuestionView.as_view(), name='ask'),
    path('visualize/', views.VisualizeView.as_view(), name='visualize'),
]