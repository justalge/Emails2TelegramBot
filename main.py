import base64
import email
import imaplib
import io
import json
import logging
import os
import re
import requests
import urllib
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv
from enum import IntEnum
from pony import orm


class NonWhiteSpace(IntEnum):
    '''Joiners, non-joiners, and separators.
    '''
    CGJ = 0x34f  # Combining Grapheme Joiner
    MVS = 0x180e  # Mongolian vowel separator
    ZWSP = 0x200b  # Zero-width space
    ZWJN = 0x200c  # Zero-width non-joiner
    ZWJ = 0x200d  # Zero-width joiner
    WJ = 0x2060  # Word joiner

    # Byte Order Mark, formerly ZWNBSP (zero-width non-breaking space):
    BOM = 0xfeff


MARKDOWN_chars = r"_*[\]()~`>#+-=|{}.!"
URL_chars = r"-a-zA-Z0-9._~:/\#@!$&'*+,;=%"  # all except "?"
SPACE_FORMAT_chars = "".join(list(map(chr, [e.value for e in NonWhiteSpace])))

load_dotenv()  # take environment variables from .env.

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
log_format = "%(levelname)s - %(asctime)s - %(message)s"
formatter = logging.Formatter(fmt=log_format,
                              datefmt="%Y-%m-%d - %H:%M:%S")
fh = logging.FileHandler("e2t.log", "w")
fh.setLevel(logging.DEBUG)
fh.setFormatter(formatter)
logger.addHandler(fh)


db = orm.Database()


class Chats(db.Entity):
    chat_id = orm.PrimaryKey(int)
    state = orm.Required(int)
    login = orm.Optional(str)
    passwd = orm.Optional(str)


db.bind(provider="sqlite", filename="db.sqlite", create_db=True)
db.generate_mapping(create_tables=True)


TOKEN = os.environ.get("API_TOKEN")
URL = "https://api.telegram.org/bot{}/".format(TOKEN)


MESSAGE_START = """Welcome to Emails2Telegram bot!
It allows you to receive emails from your \
mailbox right into this Telegram chat.

To add a mailbox you want to receive messages from send /new

To stop receive messages from current active mailbox send /stop"""
MESSAGE_GET_EMAIL = "Enter your email"
MESSAGE_GET_PASSW = """Enter your APPLICATION password
(google how to generate application password for your mailbox)"""
MESSAGE_OK = "Done!"
MESSAGE_STOP = """Your mailbox is disconnected from the chatbot now.

To connect the chatbot to your mailbox again send /new"""
MESSAGE_INVALID_CRED = """You entered invalid credentials

Make sure that you entered application password and not human one, \
google how to generate application password for your mailbox.

Try send /new and enter valid credentials again"""
MESSAGE_CONTENT = """From: {0}
Subject: {1}
-------------------

{2}"""


def make_markdown(text):
    """Escape MARKDOWN symbols and create MARKDOWN hyperlinks"""

    class LinksCounter:
        def __init__(self, links_dict):
            self.count = 0
            self.links_dict = links_dict
            self.ix = -1
            self.link_to_num = True

        def __call__(self, match):
            if self.link_to_num:
                self.count += 1
                return "FuckBidenLink{0}BidenIsFuckedLink".format(self.count)
            else:
                self.ix += 1
                nice_url = self.links_dict[self.ix][0]
                # escape markdown characters from url:
                nice_url = re.sub(fr"([{MARKDOWN_chars}])", r"\\\1", nice_url)
                full_url = self.links_dict[self.ix][0]\
                    + self.links_dict[self.ix][1]
                return f"[{nice_url}]({full_url})" if len(nice_url) < 60\
                    else f"[longURL]({full_url})"

    # get rid of ugly links:
    link_pattern = fr"(http[s]?://[{URL_chars}]+)(\?[{URL_chars}]*)?"
    all_links = re.findall(link_pattern, text)
    linksCounter = LinksCounter(all_links)
    text = re.sub(link_pattern, linksCounter, text)

    # escape markdown characters:
    text = re.sub(fr"([{MARKDOWN_chars}])", r"\\\1", text)

    # insert nice links:
    linksCounter.link_to_num = False
    p = r"FuckBidenLink(.*)BidenIsFuckedLink"
    text = re.sub(p, linksCounter, text)

    # get rid of multiple linebreaks:
    text = re.sub(fr"[ \t\f{SPACE_FORMAT_chars}]*(\r\n|\r|\n)", r"\n", text)
    text = re.sub(r"(\r\n|\r|\n){2,}", r"\n\n", text)
    return text


def decode_bytes(s):
    """Decode bytes to string"""
    encoded_tuple = email.header.decode_header(s)[0]
    decoded_string = encoded_tuple[0].decode(encoded_tuple[1], "replace") \
        if encoded_tuple[1] else encoded_tuple[0]
    return decoded_string


def get_bytes(part):
    bytes_encoded = part.get_payload()\
        .encode("utf-8")
    bytes_decoded = base64.decodebytes(bytes_encoded)
    return bytes_decoded


def get_url(url):
    response = requests.get(url, timeout=30)
    content = response.content.decode("utf8")
    return content


def get_json_from_url(url):
    content = get_url(url)
    js = json.loads(content)
    return js


def get_updates(offset=None):
    url = URL + "getUpdates?timeout=10"
    if offset:
        url += "&offset={}".format(offset)
    js = get_json_from_url(url)
    return js


def group_updates(updates):
    grouped_updates = defaultdict(lambda: [])
    for update in updates["result"]:
        message_update = update.get("message")
        if message_update:
            chat = message_update["chat"]["id"]
            grouped_updates[chat] += [update]
    return grouped_updates


def get_last_update_id(updates):
    update_ids = []
    for update in updates["result"]:
        update_ids.append(int(update["update_id"]))
    return max(update_ids)


def send_message(text, chat_id):
    TEXT_LIMIT = 4096

    logger.debug(text)
    text = make_markdown(text)

    # split message into blocks with size less then TEXT_LIMIT:
    ixes = [(m.start(0), m.end(0)) for m in re.finditer(r"\s+", text)]
    blocks, total_size = [], 0
    for i in range(len(ixes) - 1):
        s = ixes[i][0] - total_size
        e = ixes[i][1] - total_size
        s_next = ixes[i + 1][0] - total_size
        if s_next >= TEXT_LIMIT:
            blocks += [text[:s]]
            text = text[e:]
            total_size += e
    if len(text) <= TEXT_LIMIT:
        blocks += [text]
    else:
        last_s, last_e = ixes[i + 1][0], ixes[i + 1][1]
        blocks += [text[:last_s]]
        blocks += [text[last_e:]]

    # send message for each block:
    for block in blocks:
        logger.debug(block)
        url_encoded = urllib.parse.quote_plus(block)
        api_params = ["parse_mode=MarkdownV2",
                      "disable_web_page_preview=True"]
        url = URL + "sendMessage?text={}&chat_id={}&"\
            .format(url_encoded, chat_id) + "&".join(api_params)
        get_url(url)


def send_file(file_name, file_bytes, chat_id):
    with io.BytesIO() as buf:
        buf.write(file_bytes)
        buf.seek(0)
        response = requests.post(URL + "sendDocument",
                                 data={"chat_id": chat_id},
                                 files={"document": (file_name, buf)},
                                 timeout=30)
    return response.status_code == 200


def no_text_plain(now, mes_content_type):
    logger.debug(f"Message time: {now}")
    logger.debug(f"Not plain text content type: {mes_content_type}")
    return "This message has only HTML content, can't render it"


def text_plain(mes):
    payload_bytes = mes.get_payload(decode=True)
    if payload_bytes and payload_bytes.strip():
        charset = mes.get_content_charset("utf-8")
        return payload_bytes.decode(charset, "replace")
    else:
        return "No content"


def handle_part(part, now):
    content_type = part.get_content_type()
    logger.debug("<--handle part--> - "
                 + f"Message time: {now} - "
                 + f"Content type: {content_type}")
    trans_enc = part["Content-Transfer-Encoding"]
    filename = part.get_filename()
    return content_type, trans_enc, filename


def file_bytes(part, fname):
    fname = decode_bytes(fname)
    bytes_data = get_bytes(part)
    return (fname, bytes_data)


def get_new_emails(imap_login, imap_password):
    ix = imap_login.index("@")
    EMAIL = imap_login
    PASSWORD = imap_password
    SERVER = "imap." + imap_login[ix + 1:]
    if imap_login[ix + 1:] == "bk.ru":
        SERVER = "imap.mail.ru"
    elif imap_login[ix + 1:] == "phystech.edu":
        SERVER = "imap.gmail.com"
    mail = imaplib.IMAP4_SSL(SERVER)
    mail.login(EMAIL, PASSWORD)
    mail.select("inbox", readonly=False)

    status, data = mail.search(None, "UNSEEN")
    mail_ids = []
    for block in data:
        mail_ids += block.split()

    result = []

    for i in mail_ids:
        status, data = mail.fetch(i, "(RFC822)")
        # the content data at the "(RFC822)" format comes on
        # a list with a tuple with header, content, and the closing
        # byte b")"
        for response_part in data:
            if isinstance(response_part, tuple):
                # we go for the content at its second element
                # skipping the header at the first and the closing
                # at the third:
                message = email.message_from_bytes(response_part[1])
                mes_content_type = message.get_content_type()
                typ, data = mail.store(i, "+FLAGS", "\\Seen")
                now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                logger.info(f'{now} {mes_content_type}')

                mail_from = decode_bytes(message["from"])
                mail_subject = decode_bytes(message["subject"])

                files_attached = []

                if message.is_multipart():
                    no_pl_txt = True
                    for part in message.get_payload():
                        contype, transfenc, fname = handle_part(part, now)
                        if contype == "text/plain":
                            no_pl_txt = False
                            mail_content = text_plain(part)
                        elif part.is_multipart():
                            for p in part.get_payload():
                                ctype, tenc, fn = handle_part(p, now)
                                if ctype == "text/plain":
                                    no_pl_txt = False
                                    mail_content = text_plain(p)
                                elif tenc == "base64" and fn:
                                    files_attached += [file_bytes(p, fn)]
                        elif transfenc == "base64" and fname:
                            files_attached += [file_bytes(part, fname)]
                    if no_pl_txt:
                        mail_content = no_text_plain(now, mes_content_type)
                elif mes_content_type == "text/plain":
                    mail_content = text_plain(message)
                else:
                    mail_content = no_text_plain(now, mes_content_type)

                result += [{"from": mail_from, "subj": mail_subject,
                            "content": mail_content,
                            "attachment": files_attached}]
    mail.close()
    return result


@orm.db_session()
def handle_updates(grouped_updates):
    for chat_id, g_upd in grouped_updates.items():
        current_chat = Chats.get(chat_id=chat_id)
        current_state = current_chat.state if current_chat else 0
        for upd in g_upd:
            text = upd["message"]["text"]
            if text == "/start" and current_state == 0:
                send_message(MESSAGE_START, chat_id)
            elif text == "/new" and current_state == 0:
                current_state = 1
                if not current_chat:
                    Chats(chat_id=chat_id, state=current_state)
                else:
                    current_chat.state = current_state
                send_message(MESSAGE_GET_EMAIL, chat_id)
            elif current_chat and current_state == 1:
                current_chat.state = 2
                current_chat.login = text
                send_message(MESSAGE_GET_PASSW, chat_id)
            elif current_chat and current_state == 2:
                current_chat.state = 0
                current_chat.passwd = text
                send_message(MESSAGE_OK, chat_id)
            elif text == "/stop" and current_state == 0:
                if current_chat:
                    current_chat.delete()
                send_message(MESSAGE_STOP, chat_id)


@orm.db_session()
def main():
    last_update_id = None
    while True:
        updates = get_updates(last_update_id)
        if len(updates["result"]) > 0:
            last_update_id = get_last_update_id(updates) + 1
            grouped_updates = group_updates(updates)
            handle_updates(grouped_updates)
        to_broadcast = orm.select(c for c in Chats)[:]
        for c in to_broadcast:
            res = []
            fail_respond = None
            if c.login and c.passwd:
                try:
                    res = get_new_emails(c.login, c.passwd)
                except Exception:
                    fail_respond = MESSAGE_INVALID_CRED
                    logger.exception("get_new_email failed :(")

            if fail_respond:
                send_message(fail_respond, c.chat_id)
                c.delete()
            else:
                for e in res:
                    respond = MESSAGE_CONTENT.format(e["from"],
                                                     e["subj"], e["content"])

                    send_message(respond, c.chat_id)
                    for f in e["attachment"]:
                        send_file(f[0], f[1], c.chat_id)
        orm.commit()


if __name__ == "__main__":
    main()
