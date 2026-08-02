from django.urls import path
from . import views

urlpatterns = [
    path('', views.UploadFileView.as_view(), name='upload'),
    path('ask/', views.AskQuestionView.as_view(), name='ask'),
    # path('table-info/', views.get_table_info, name='table_info'),
]