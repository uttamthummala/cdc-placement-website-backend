import datetime
import json
import logging
import os
import random
import re
import string
import sys
import traceback
from os import path, remove

import background_task
import jwt
import pdfkit
import pytz
import requests as rq
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.forms.models import model_to_dict
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone
from google.auth.transport import requests
from google.oauth2 import id_token
from rest_framework import status
from rest_framework.response import Response

from .constants import *
from .models import User, PrePlacementOffer, PlacementApplication, Placement, Student, Internship,InternshipApplication

logger = logging.getLogger('db')


def get_token():
    def decorator(view_func):
        def wrapper_func(request, *args, **kwargs):
            try:
                authcode = request.data[AUTH_CODE]
                data = {
                    'code': authcode,
                    'client_id': CLIENT_ID,
                    'client_secret': CLIENT_SECRET,
                    'redirect_uri': REDIRECT_URI,
                    'grant_type': 'authorization_code'
                }
                r = rq.post(OAUTH2_API_ENDPOINT, data=data)
                if r.status_code == 200:
                    response = r.json()
                    token = response[ID_TOKEN]
                    refresh_token = response[REFRESH_TOKEN]
                    request.META["HTTP_AUTHORIZATION"] = "Bearer " + token
                    request.META["MODIFIED"] = "True"
                    kwargs['refresh_token'] = refresh_token
                    return view_func(request, *args, **kwargs)
                else:
                    return Response({'action': "Get Token", 'message': "Invalid Auth Code"},
                                    status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logger.warning("Get Token: " + str(sys.exc_info()))
                return Response({'action': "Get Token", 'message': str(e)},
                                status=status.HTTP_400_BAD_REQUEST)

        return wrapper_func

    return decorator


def precheck(required_data=None):
    if required_data is None:
        required_data = []

    def decorator(view_func):
        def wrapper_func(request, *args, **kwargs):
            try:
                request_data = None
                if request.method == 'GET':
                    request_data = request.GET
                elif request.method == 'POST':
                    request_data = request.data
                    if not len(request_data):
                        request_data = request.POST
                if len(request_data):
                    for i in required_data:
                        # print(i)
                        if i not in request_data:
                            return Response({'action': "Pre check", 'message': str(i) + " Not Found"},
                                            status=status.HTTP_400_BAD_REQUEST)
                else:
                    return Response({'action': "Pre check", 'message': "Message Data not Found"},
                                    status=status.HTTP_400_BAD_REQUEST)
                # print("Pre check: " + str(request_data))
                return view_func(request, *args, **kwargs)
            except:
                # print what exception is
                print(traceback.format_exc())
                logger.warning("Pre check: " + str(sys.exc_info()))
                return Response({'action': "Pre check", 'message': "Something went wrong"},
                                status=status.HTTP_400_BAD_REQUEST)

        return wrapper_func

    return decorator


def isAuthorized(allowed_users=None):
    if allowed_users is None:
        allowed_users = []

    def decorator(view_func):
        def wrapper_func(request, *args, **kwargs):
            try:
                headers = request.META
                if 'HTTP_AUTHORIZATION' in headers:
                    token_id = headers['HTTP_AUTHORIZATION'][7:]
                    idinfo = id_token.verify_oauth2_token(token_id, requests.Request(), CLIENT_ID)
                    email = idinfo[EMAIL]
                    user = get_object_or_404(User, email=email)
                    if user:
                        user.last_login_time = timezone.now()
                        user.save()
                        if len(set(user.user_type).intersection(set(allowed_users))) or allowed_users == '*':
                            if "MODIFIED" in headers:
                                return view_func(request, user.id, user.email, user.user_type, token_id, *args,
                                                 **kwargs)
                            else:
                                return view_func(request, user.id, user.email, user.user_type, *args, **kwargs)
                        else:
                            raise PermissionError("Access Denied. You are not allowed to use this service")
                else:
                    raise PermissionError("Authorization Header Not Found")

            except PermissionError:
                return Response({'action': "Is Authorized?", 'message': 'Access Denied'},
                                status=status.HTTP_401_UNAUTHORIZED)
            except Http404:
                return Response({'action': "Is Authorized?", 'message': "User Not Found. Contact CDC for more details"},
                                status=status.HTTP_404_NOT_FOUND)
            except ValueError as e:
                logger.error("Problem with Google Oauth2.0 " + str(e))
                return Response({'action': "Is Authorized?", 'message': 'Problem with Google Sign In'},
                                status=status.HTTP_401_UNAUTHORIZED)
            except:
                logger.warning("Is Authorized? " + str(sys.exc_info()))
                return Response(
                    {'action': "Is Authorized?", 'message': "Something went wrong. Contact CDC for more details"},
                    status=status.HTTP_400_BAD_REQUEST)

        return wrapper_func

    return decorator


def generateRandomString():
    try:
        N = 15
        res = ''.join(random.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=N))
        return res
    except:
        return False


def saveFile(file, location):
    prefix = generateRandomString()
    file_name = prefix + "_" + file.name.strip()

    file_name = re.sub(r'[\\/:*?"<>|]', '_', file_name)

    if not path.isdir(location):
        os.makedirs(location)

    destination_path = location + str(file_name)
    if path.exists(destination_path):
        remove(destination_path)

    with open(destination_path, 'wb+') as destination:
        for chunk in file.chunks():
            destination.write(chunk)
    return file_name


@background_task.background(schedule=2)
def sendEmail(email_to, subject, data, template, attachment_jnf_response=None):
    try:
        if not isinstance(data, dict):
            data = json.loads(data)
        html_content = render_to_string(template, data)  # render with dynamic value
        text_content = strip_tags(html_content)

        email_from = settings.EMAIL_HOST_USER
        if type(email_to) is list:
            recipient_list = [str(email) for email in email_to]
        else:
            recipient_list = [str(email_to), ]

        msg = EmailMultiAlternatives(subject, text_content, email_from,None,bcc=recipient_list)
        msg.attach_alternative(html_content, "text/html")
        if attachment_jnf_response:
            # logger.info(attachment_jnf_response)
            pdf = pdfkit.from_string(attachment_jnf_response['html'], False,
                                     options={"--enable-local-file-access": "", '--dpi': '96'})
            msg.attach(attachment_jnf_response['name'], pdf, 'application/pdf')
        msg.send()
        return True
    except:
        logger.error("Send Email: " + str(sys.exc_info()))
        return False


def PlacementApplicationConditions(student, placement):
    try:
        selected_companies = PlacementApplication.objects.filter(student=student, selected=True)
        selected_companies_PSU = [i for i in selected_companies if i.placement.tier == 'psu']
        PPO = PrePlacementOffer.objects.filter(student=student, accepted=True)
        PPO_PSU = [i for i in PPO if i.tier == 'psu']
        # find length of PPO
        if len(selected_companies) + len(PPO) >= MAX_OFFERS_PER_STUDENT:
            raise PermissionError("Max Applications Reached for the Season")

        if len(selected_companies_PSU) > 0:
            raise PermissionError('Selected for PSU Can\'t apply anymore')

        if len(PPO_PSU) > 0:
            raise PermissionError('Selected for PSU Can\'t apply anymore')

        if placement.tier == 'psu':
            return True, "Conditions Satisfied"

        for i in selected_companies:
            if int(i.placement.tier) < int(placement.tier):
                return False, "Can't apply for this tier"

        for i in PPO:
            if int(i.tier) < int(placement.tier):
                return False, "Can't apply for this tier"

        if student.degree != 'bTech' and not placement.rs_eligible:
            raise PermissionError("Can't apply for this placement")

        return True, "Conditions Satisfied"

    except PermissionError as e:
        return False, e
    except:
        logger.warning("Utils - PlacementApplicationConditions: " + str(sys.exc_info()))
        return False, "_"

def InternshipApplicationConditions(student, internship):
    try:
        selected_companies = InternshipApplication.objects.filter(student=student, selected=True)
        if len(selected_companies)>=1:
           # print("selected companies > 1")
            return False, "You have already secured a Internship"
        return True, "Conditions Satisfied"

    except PermissionError as e:
        return False, e
    except:
        logger.warning("Utils - InternshipApplicationConditions: " + str(sys.exc_info()))
        return False, "_"


def getTier(compensation_gross, is_psu=False):
    try:
        if is_psu:
            return True, 'psu'
        if compensation_gross < 0:
            raise ValueError("Negative Compensation")
        elif compensation_gross < 450000:  # Open Tier If less than 450,000
            return True, "8"
        elif compensation_gross < 600000:  # Tier 7 If less than 600,000
            return True, "7"
        # Tier 6 If less than 800,000 and greater than or equal to 600,000
        elif compensation_gross < 800000:
            return True, "6"
        # Tier 5 If less than 1,000,000 and greater than or equal to 800,000
        elif compensation_gross < 1000000:
            return True, "5"
        # Tier 4 If less than 1,200,000 and greater than or equal to 1,000,000
        elif compensation_gross < 1200000:
            return True, "4"
        # Tier 3 If less than 1,500,000 and greater than or equal to 1,200,000
        elif compensation_gross < 1500000:
            return True, "3"
        # Tier 2 If less than 1,800,000 and greater than or equal to 1,500,000
        elif compensation_gross < 1800000:
            return True, "2"
        # Tier 1 If greater than or equal to 1,800,000
        elif compensation_gross >= 1800000:
            return True, "1"
        else:
            raise ValueError("Invalid Compensation")

    except ValueError as e:
        logger.warning("Utils - getTier: " + str(sys.exc_info()))
        return False, e
    except:
        logger.warning("Utils - getTier: " + str(sys.exc_info()))
        return False, "_"


def generateOneTimeVerificationLink(email, opening_id, opening_type):
    try:
        token_payload = {
            "email": email,
            "opening_id": opening_id,
            "opening_type": opening_type,
            'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=EMAIL_VERIFICATION_TOKEN_TTL)
        }
        token = jwt.encode(token_payload, os.environ.get("EMAIL_VERIFICATION_SECRET_KEY"), algorithm="HS256")
        link = LINK_TO_EMAIl_VERIFICATION_API.format(token=token)
        return True, link
    except:
        logger.warning("Utils - generateOneTimeVerificationLink: " + str(sys.exc_info()))
        return False, "_"


def verify_recaptcha(request):
    try:
        data = {
            'secret': settings.RECAPTCHA_SECRET_KEY,
            'response': request
        }
        r = rq.post('https://www.google.com/recaptcha/api/siteverify', data=data)
        result = r.json()
        if not result['success']:
            logger.warning("Utils - verify_recaptcha: " + str(result))
        return result['success']
    except:
        # get exception line number
        logger.warning("Utils - verify_recaptcha: " + str(sys.exc_info()))
        return False, "_"


def opening_description_table_html(opening):
    # check typing of opening
    type = ""
    if isinstance(opening, Placement):
        type = "Job"
        details = model_to_dict(opening, fields=[field.name for field in Placement._meta.fields],
                                exclude=EXCLUDE_IN_PDF)
    elif isinstance(opening, Internship):
        type = "Internship"
        details = model_to_dict(opening, fields=[field.name for field in Internship._meta.fields],
                                exclude=EXCLUDE_IN_PDF)
    # check typing of opening is query dict
    else:  # if isinstance(opening, QueryDict):
        details = opening
    keys = list(details.keys())
    newdetails = {"ID": opening.id}
    for key in keys:
        if isinstance(details[key], list):
            details[key] = {"details": details[key], "type": ["list"]}
        if key in SPECIAL_FORMAT_IN_PDF:
            if key == 'website':
                details[key] = {"details": details[key], "type": ["link"]}
            else:
                details[key] = {"details": [item for item in details[key]["details"]], "type": ["list", "link"],
                                "link": PDF_FILES_SERVING_ENDPOINT + opening.id + "/"}
        new_key = key.replace('_', ' ')
        if new_key.endswith(' names'):
            new_key = new_key[:-6]
        new_key = new_key.capitalize()
        newdetails[new_key] = details[key]
    imagepath = os.path.abspath('./templates/image.png')
    data = {
        "data": newdetails,
        "imgpath": imagepath,
        "type": type
    }
    return render_to_string(COMPANY_JNF_RESPONSE_TEMPLATE, data)


def placement_eligibility_filters(student, placements):
    try:
        filtered_placements = []
        for placement in placements.iterator():

            if PlacementApplicationConditions(student, placement)[0]:
                filtered_placements.append(placement)

        return filtered_placements
    except:
        logger.warning("Utils - placement_eligibility_filters: " + str(sys.exc_info()))
        return placements
def internship_eligibility_filters(student, internships):
    try:
        filtered_internships = []
        for internship in internships.iterator():

            if InternshipApplicationConditions(student, internship)[0]:
                filtered_internships.append(internship)

        return filtered_internships
    except:
        logger.warning("Utils - internship_eligibility_filters: " + str(sys.exc_info()))
        return internships


@background_task.background(schedule=2)
def send_opening_notifications(opening_id, opening_type=PLACEMENT):
    try:
       # print(opening_id, opening_type)
        if opening_type == PLACEMENT:
            opening = get_object_or_404(Placement, id=opening_id)
        else:
            opening = get_object_or_404(Internship, id=opening_id)
        emails=[]
        students = Student.objects.all()
        for student in students.iterator():
            if student.branch in opening.allowed_branch:
                if student.degree == 'bTech' or opening.rs_eligible is True:
                    if (isinstance(opening,Placement) and PlacementApplicationConditions(student, opening)[0]) or (isinstance(opening,Internship) and InternshipApplicationConditions(student, opening)[0]):
                        try:
                            student_user = get_object_or_404(User, id=student.id)
                            emails.append(student_user.email)
                            #sendEmail(student_user.email, subject, data, NOTIFY_STUDENTS_OPENING_TEMPLATE)
                        except Http404:
                            logger.warning('Utils - send_opening_notifications: user not found : ' + student.id)
                        except Exception as e:
                            logger.warning('Utils - send_opening_notifications: For Loop' + str(e))
        subject = NOTIFY_STUDENTS_OPENING_TEMPLATE_SUBJECT.format(
                                company_name=opening.company_name)
        deadline_datetime = opening.deadline_datetime.astimezone(pytz.timezone('Asia/Kolkata'))
        data = {
            "company_name": opening.company_name,
            "opening_type": "INTERNSHIP" if isinstance(opening, Internship) else "PLACEMENT",
            "designation": opening.designation,
            "deadline": deadline_datetime.strftime("%A, %-d %B %Y, %-I:%M %p"),
            "link": PLACEMENT_OPENING_URL.format(id=opening.designation) if opening_type == PLACEMENT else INTERNSHIP_OPENING_URL.format(id=opening.designation),
            }                            
        sendEmail(emails, subject, data, NOTIFY_STUDENTS_OPENING_TEMPLATE) #handled multiple mailings
    except:
        logger.warning('Utils - send_opening_notifications: ' + str(sys.exc_info()))
        return False


def exception_email(opening):
    opening = opening.dict()
    data = {
        "designation": opening["designation"],
        "opening_type": "INTERNSHIP" if opening["opening_type"] == "INF" else "PLACEMENT",
        "company_name": opening["company_name"],
    }
    pdfhtml = opening_description_table_html(opening)
    name = opening["company_name"] + '_jnf_response.pdf' if opening[OPENING_TYPE]!="INF" else opening["company_name"] + '_inf_response.pdf'
    attachment_jnf_respone = {
        "name": name,
        "html": pdfhtml,
    }

    sendEmail("cdc@iitdh.ac.in", COMPANY_OPENING_ERROR_TEMPLATE.format(company_name=opening["company_name"]), data,
              COMPANY_OPENING_SUBMITTED_TEMPLATE, attachment_jnf_respone)


def store_all_files(request):
    files = request.FILES
    data = request.data
    # save all the files
    if files:
        # company details pdf
        for file in files.getlist(COMPANY_DETAILS_PDF):
            file_location = STORAGE_DESTINATION_COMPANY_ATTACHMENTS + "temp" + '/'
            saveFile(file, file_location)
        # compensation details pdf
        for file in files.getlist(COMPENSATION_DETAILS_PDF):
            file_location = STORAGE_DESTINATION_COMPANY_ATTACHMENTS + "temp" + '/'
            saveFile(file, file_location)
        #stipend details pdf for internships
        for file in files.getlist(STIPEND_DETAILS_PDF):
            file_location = STORAGE_DESTINATION_COMPANY_ATTACHMENTS + "temp" + '/'
            saveFile(file, file_location)
        # selection procedure details pdf
        for file in files.getlist(SELECTION_PROCEDURE_DETAILS_PDF):
            file_location = STORAGE_DESTINATION_COMPANY_ATTACHMENTS + "temp" + '/'
            saveFile(file, file_location)
        # description pdf
        for file in files.getlist(DESCRIPTION_PDF):
            file_location = STORAGE_DESTINATION_COMPANY_ATTACHMENTS + "temp" + '/'
            saveFile(file, file_location)


def send_email_for_opening(opening):
    try:

        # Prepare email data and attachment
        pdfhtml = opening_description_table_html(opening)
        if isinstance(opening, Placement):
            name = opening.company_name + '_jnf_response.pdf'
        elif isinstance(opening, Internship):
            name = opening.company_name + '_inf_response.pdf'
        attachment_jnf_respone = {
            "name": name,
            "html": pdfhtml,
        }
        data = {
            "designation": opening.designation,
            "opening_type": "INTERNSHIP" if isinstance(opening, Internship) else "PLACEMENT",
            "company_name": opening.company_name,
        }

        emails = [opening.email] + CDC_REPS_EMAILS
        # Send the email
        sendEmail(emails,
                  COMPANY_OPENING_SUBMITTED_TEMPLATE_SUBJECT.format(id=opening.designation, company=opening.company_name), data,
                  COMPANY_OPENING_SUBMITTED_TEMPLATE, attachment_jnf_respone)

    except Exception as e:
        # Handle the exception here (e.g., log the error, send an error email, etc.)
        print("An error occurred while sending the email:", e)


