import datetime
import logging
import os
import re

from google.appengine.api.app_identity import get_default_version_hostname
from google.appengine.api.mail import EmailMessage
from google.appengine.api import memcache
import jinja2
import webapp2

import evelink
from evelink import appengine as elink_appengine
from models import Configuration, SeenMail, SeenNotification, NotificationTypes

jinja_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__)))

class HomeHandler(webapp2.RequestHandler):

  def get(self):
    config = memcache.get('config') or Configuration.all().get()
    notify_descriptions = (memcache.get('ndesc') or
                           NotificationTypes.all())

    if config:
      template_values = {
        'key_id': config.key_id,
        'vcode': config.vcode,
        'rcpt_char': config.rcpt_char,
        'rcpt_org': config.rcpt_org,
        'rcpt_org2': config.rcpt_org2 or '',
        'dest_email': config.dest_email,
        'notify_types': config.notify_types,
        'notify_descriptions': notify_descriptions,
      }
    else:
      template_values = {
        'key_id': '',
        'vcode': '',
        'rcpt_char': '',
        'rcpt_org': '',
        'rcpt_org2': '',
        'dest_email': '',
        'notify_types': [],
        'notify_descriptions': notify_descriptions,
      }

    template = jinja_environment.get_template('index.html')
    self.response.out.write(template.render(template_values))

  def post(self):
    key_id = self.request.get('key_id')
    vcode = self.request.get('vcode')
    rcpt_char = self.request.get('rcpt_char')
    rcpt_org = self.request.get('rcpt_org')
    rcpt_org2 = self.request.get('rcpt_org2')
    notify_types = [int(x) for x in self.request.get_all('notify_types')]
    dest_email = self.request.get('dest_email')

    if not (key_id and vcode and rcpt_org and dest_email):
      self.response.out.write("Missing one or more fields.")
      return

    try:
      key_id = int(key_id)
    except (ValueError, TypeError):
      self.response.out.write("Key ID must be an integer.")
      return

    try:
      rcpt_char = int(rcpt_char)
    except (ValueError, TypeError):
      rcpt_char = self.get_entity_id(rcpt_char)

    try:
      rcpt_org = int(rcpt_org)
    except (ValueError, TypeError):
      rcpt_org = self.get_entity_id(rcpt_org)

    if not rcpt_org:
      self.response.out.write("Invalid organization name/id.")
      return

    if rcpt_org2:
      try:
        rcpt_org2 = int(rcpt_org2)
      except (ValueError, TypeError):
        rcpt_org2 = self.get_entity_id(rcpt_org2)

      if not rcpt_org2:
        self.response.out.write("Invalid organization #2 name/id.")
        return

    config = Configuration.all().get()
    if not config:
      config = Configuration(
        key_id=key_id,
        vcode=vcode,
        rcpt_char=rcpt_char,
        rcpt_org=rcpt_org,
        rcpt_org2=rcpt_org2,
        notify_types=notify_types,
        dest_email=dest_email,
      )
    else:
      config.key_id = key_id
      config.vcode = vcode
      config.rcpt_char = rcpt_char
      config.rcpt_org = rcpt_org
      if rcpt_org2:
        config.rcpt_org2 = rcpt_org2
      config.notify_types = notify_types
      config.dest_email = dest_email

    config.put()
    memcache.set('config', config)

    self.response.out.write("Configuration saved.")
    return

  def get_entity_id(self, entity_name):
    got_name = memcache.get('name-%s' % entity_name)
    if got_name:
      return got_name
    elink_api = elink_appengine.AppEngineAPI()
    elink_eve = evelink.eve.EVE(api=elink_api)
    got_name = elink_eve.character_id_from_name(entity_name)
    if got_name:
      memcache.set('name-%s' % entity_name, got_name)
    return got_name


class CronHandler(webapp2.RequestHandler):

  def get(self):
    config = memcache.get('config') or Configuration.all().get()
    notify_descriptions = (memcache.get('ndesc') or
                           NotificationTypes.all())

    if not config:
      # We haven't set up our configuration yet, so don't try to do anything
      return

    elink_api = elink_appengine.AppEngineAPI(api_key=(config.key_id, config.vcode))
    elink_char = evelink.char.Char(config.rcpt_char, api=elink_api)
    elink_eve = evelink.eve.EVE(api=elink_api)

    self.send_emails(config, elink_api, elink_char, elink_eve)
    self.send_notifications(config, elink_api, elink_char, elink_eve,
                            notify_descriptions)

  def send_emails(self, config, elink_api, elink_char, elink_eve):

    recips = set([config.rcpt_org])
    if config.rcpt_org2:
      recips.add(config.rcpt_org2)

    headers = elink_char.messages()
    message_ids = set(h['id'] for h in headers if h['to']['org_id'] in recips)

    headers = dict((h['id'], h) for h in headers)

    message_ids_to_relay = set()
    sender_ids = set()

    for m_id in message_ids:
      seen = memcache.get('seen-%s' % m_id) or SeenMail.gql("WHERE mail_id = :1", m_id).get()
      if not seen:
        message_ids_to_relay.add(m_id)
        sender_ids.add(headers[m_id]['sender_id'])
      else:
        memcache.set('seen-%s' % m_id, True)

    if not message_ids_to_relay:
      self.response.out.write("No pending messages.<br/>")
      return

    bodies = elink_char.message_bodies(message_ids_to_relay)
    senders = elink_eve.character_names_from_ids(sender_ids)

    e = EmailMessage()
    e.to = config.dest_email
    e.sender = 'no-reply@evemail-bridge.appspotmail.com'
    for m_id in message_ids_to_relay:
      sender = senders[headers[m_id]['sender_id']]
      timestamp = headers[m_id]['timestamp']
      e.subject = '[EVEMail] %s' % headers[m_id]['title']
      e.html = self.format_message(bodies[m_id], timestamp, sender)
      e.send()
      SeenMail(mail_id=m_id).put()
      memcache.set('seen-%s' % m_id, True)
      self.response.out.write("Processed message ID %s.<br/>\n" % m_id)

    return

  def send_notifications(self, config, elink_api, elink_char, elink_eve,
                         notify_descriptions):

    headers = elink_char.notifications()
    message_ids = set(headers[h]['id'] for h in headers
                      if headers[h]['type_id'] in config.notify_types )

    headers = dict((headers[h]['id'], headers[h]) for h in headers)

    message_ids_to_relay = set()
    sender_ids = set()

    for m_id in message_ids:
      seen = (memcache.get('nseen-%s' % m_id) or
              SeenNotification.gql("WHERE notify_id = :1", m_id).get())
      if not seen:
        message_ids_to_relay.add(m_id)
        sender_ids.add(headers[m_id]['sender_id'])
      else:
        memcache.set('nseen-%s' % m_id, True)

    if not message_ids_to_relay:
      self.response.out.write("No pending notifications.<br/>")
      return

    bodies = elink_char.notification_texts(message_ids_to_relay)
    senders = elink_eve.character_names_from_ids(sender_ids)

    e = EmailMessage()
    e.to = config.dest_email
    e.sender = 'no-reply@evemail-bridge.appspotmail.com'
    for m_id in message_ids_to_relay:
      sender = senders[headers[m_id]['sender_id']]
      timestamp = headers[m_id]['timestamp']
      e.subject = ('[EVE Notify] %s' %
          notify_descriptions.filter(
              "type_id = ", headers[m_id]['type_id']).get().description)
      e.html = self.format_notification(bodies[m_id], timestamp, sender,
                                        elink_eve)
      e.send()
      SeenNotification(notification_id=m_id).put()
      memcache.set('nseen-%s' % m_id, True)
      self.response.out.write("Processed notification ID %s.<br/>\n" % m_id)

    return

  def format_message(self, body, timestamp, sender):
    mtime = datetime.datetime.fromtimestamp(timestamp)
    body = re.sub(r'</?font.*?>', r'', body)
    body = "<p>Sent by %s at %s EVE Time</p>%s" % (sender, mtime, body)
    return body

  def format_notification(self, data, timestamp, sender, elink_eve):
    mtime = datetime.datetime.fromtimestamp(timestamp)
    body = ("Attack by %s <%s> [%s] against %s located at " +
            "moon %s in system %s. Health is %f/%f/%f.") % (
                 data['aggressorID'],
                 data['aggressorCorpID'],
                 data['aggressorAllianceID'],
                 data['typeID'],
                 data['moonID'],
                 data['solarSystemID'],
                 data['shieldValue'],
                 data['armorValue'],
                 data['hullValue'],
               )
    body = "<p>Sent by %s at %s EVE Time</p>%s" % (sender, mtime, body)
    return body

class NullHandler(webapp2.RequestHandler):
  def get(self):
    pass


application = webapp2.WSGIApplication(
  [
    ('/cron', CronHandler),
    ('/favicon.ico', NullHandler),
    ('/', HomeHandler),
  ],
  debug=True,
)
