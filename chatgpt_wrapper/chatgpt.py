import argparse
import base64
import cmd
import json
import operator
import platform
import sys
import uuid
from functools import reduce
from time import sleep

# use pyreadline3 instead of readline on windows
is_windows = platform.system() == "Windows"
if is_windows:
    import pyreadline3  # noqa: F401
else:
    import readline

from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.markdown import Markdown

console = Console()


class ChatGPT:
    """
    A ChatGPT interface that uses Playwright to run a browser,
    and interacts with that browser to communicate with ChatGPT in
    order to provide an open API to ChatGPT.
    """

    stream_div_id = "chatgpt-wrapper-conversation-stream-data"
    eof_div_id = "chatgpt-wrapper-conversation-stream-data-eof"
    session_div_id = "chatgpt-wrapper-session-data"

    def __init__(self, headless: bool = True, browser = "firefox"):
        self.play = sync_playwright().start()

        try:
            playbrowser = getattr(self.play, browser)
        except Exception:
            print(f"Browser {browser} is invalid, falling back on firefox")
            playbrowser = self.play.firefox

        self.browser = playbrowser.launch_persistent_context(
            user_data_dir="/tmp/playwright",
            headless=headless,
        )
        self.page = self.browser.new_page()
        self._start_browser()
        self.parent_message_id = str(uuid.uuid4())
        self.conversation_id = None
        self.session = None

    def _start_browser(self):
        self.page.goto("https://chat.openai.com/")

    def refresh_session(self):
        self.page.evaluate(
            """
        const xhr = new XMLHttpRequest();
        xhr.open('GET', 'https://chat.openai.com/api/auth/session');
        xhr.onload = () => {
          if(xhr.status == 200) {
            var mydiv = document.createElement('DIV');
            mydiv.id = "SESSION_DIV_ID"
            mydiv.innerHTML = xhr.responseText;
            document.body.appendChild(mydiv);
          }
        };
        xhr.send();
        """.replace(
                "SESSION_DIV_ID", self.session_div_id
            )
        )

        while True:
            session_datas = self.page.query_selector_all(f"div#{self.session_div_id}")
            if len(session_datas) > 0:
                break
            sleep(0.2)

        session_data = json.loads(session_datas[0].inner_text())
        self.session = session_data

        self.page.evaluate(f"document.getElementById('{self.session_div_id}').remove()")

    def _cleanup_divs(self):
        self.page.evaluate(f"document.getElementById('{self.stream_div_id}').remove()")
        self.page.evaluate(f"document.getElementById('{self.eof_div_id}').remove()")

    def start_stream(self, prompt: str):
        if self.session is None:
            self.refresh_session()
        new_message_id = str(uuid.uuid4())
        print("hi")
        if "accessToken" not in self.session:
            yield (
                "Your ChatGPT session is not usable.\n"
                "* Run this program with the `install` parameter and log in to ChatGPT.\n"
                "* If you think you are already logged in, try running the `session` command."
            )
            return

        request = {
            "messages": [
                {
                    "id": new_message_id,
                    "role": "user",
                    "content": {"content_type": "text", "parts": [prompt]},
                }
            ],
            "model": "text-davinci-002-render",
            "conversation_id": self.conversation_id,
            "parent_message_id": self.parent_message_id,
            "action": "next",
        }

        code = (
            """
            const stream_div = document.createElement('DIV');
            stream_div.id = "STREAM_DIV_ID";
            document.body.appendChild(stream_div);
            const xhr = new XMLHttpRequest();
            const fake_div = document.createElement('DIV');
            fake_div.id = "fake_id";
            document.body.appendChild(fake_div);
            xhr.open('POST', 'https://chat.openai.com/backend-api/conversation');
            xhr.setRequestHeader('Accept', 'text/event-stream');
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.setRequestHeader('Authorization', 'Bearer BEARER_TOKEN');
            xhr.responseType = 'stream';
            xhr.onreadystatechange = function() {
              var newEvent;
              if(xhr.readyState == 3 || xhr.readyState == 4) {
                const newData = xhr.response.substr(xhr.seenBytes);
                try {
                  const newEvents = newData.split(/\\n\\n/).reverse();
                  newEvents.shift();
                  if(newEvents[0] == "data: [DONE]") {
                    newEvents.shift();
                  }
                  if(newEvents.length > 0) {
                    newEvent = newEvents[0].substring(6);
                    // using XHR for eventstream sucks and occasionally ive seen incomplete
                    // json objects come through  JSON.parse will throw if that happens, and
                    // that should just skip until we get a full response.
                    JSON.parse(newEvent);
                  }
                } catch (err) {
                  console.log(err);
                  return;
                }
                if(newEvent !== undefined) {
                  stream_div.innerHTML = btoa(newEvent);
                  xhr.seenBytes = xhr.responseText.length;
                }
              }
              if(xhr.readyState == 4) {
                const eof_div = document.createElement('DIV');
                eof_div.id = "EOF_DIV_ID";
                document.body.appendChild(eof_div);
              }
            };
            xhr.send(JSON.stringify(REQUEST_JSON));
            """.replace(
                "BEARER_TOKEN", self.session["accessToken"]
            )
            .replace("REQUEST_JSON", json.dumps(request))
            .replace("STREAM_DIV_ID", self.stream_div_id)
            .replace("EOF_DIV_ID", self.eof_div_id)
        )
        self.page.evaluate(code)
        print(self.page.query_selector_all("div#fake_id"))
        return self.page

    def ask_stream(self):
        last_event_msg = ""

        eof_datas = self.page.query_selector_all(f"div#{self.eof_div_id}")

        conversation_datas = self.page.query_selector_all(
                f"div#{self.stream_div_id}"
        )
        print(conversation_datas)
        if len(conversation_datas) == 0:
            return False

        full_event_message = None

        try:
            event_raw = base64.b64decode(conversation_datas[0].inner_html())
            if len(event_raw) > 0:
                event = json.loads(event_raw)
                if event is not None:
                    self.parent_message_id = event["message"]["id"]
                    self.conversation_id = event["conversation_id"]
                    full_event_message = "\n".join(
                        event["message"]["content"]["parts"]
                    )

        except Exception:
            return False

        if len(eof_datas) > 0:
            return "DONE"

        if full_event_message is not None:
            chunk = full_event_message[len(last_event_msg) :]
            last_event_msg = full_event_message
            return last_event_msg

    def ask(self, message: str) -> str:
        """
        Send a message to chatGPT and return the response.

        Args:
            message (str): The message to send.

        Returns:
            str: The response received from OpenAI.
        """
        list(self.start_stream(message)) # the list call makes sure start_stream is completed
        sleep(20)
        print(self.ask_stream())
        #response = list(self.start_stream(message))
        # return (
        #     reduce(operator.add, response)
        #     if len(response) > 0
        #     else "Unusable response produced by ChatGPT, maybe its unavailable."
        # )

    def new_conversation(self):
        self.parent_message_id = str(uuid.uuid4())
        self.conversation_id = None

# def main():

#     parser = argparse.ArgumentParser()
#     parser.add_argument(
#         "params",
#         nargs="*",
#         help="Use 'install' for install mode, or provide a prompt for ChatGPT.",
#     )
#     parser.add_argument(
#         "-s", "--stream", action="store_true", help="enable streaming mode"
#     )
#     parser.add_argument(
#         "-l",
#         "--log",
#         action="store",
#         help="log prompts and responses to the named file",
#     )
#     parser.add_argument(
#         "-b",
#         "--browser",
#         action="store",
#         help="set preferred browser; 'firefox' 'chromium' or 'webkit'",
#     )
#     args = parser.parse_args()
#     install_mode = len(args.params) == 1 and args.params[0] == "install"

#     if install_mode:
#         print(
#             "Install mode: Log in to ChatGPT in the browser that pops up, and click\n"
#             "through all the dialogs, etc. Once that is acheived, exit and restart\n"
#             "this program without the 'install' parameter.\n"
#         )

#     extra_kwargs = {} if args.browser is None else {"browser": args.browser}
#     chatgpt = ChatGPT(headless=not install_mode, **extra_kwargs)

#     shell = GPTShell()
#     shell._set_chatgpt(chatgpt)
#     shell._set_args(args)

#     if len(args.params) > 0 and not install_mode:
#         shell.default(" ".join(args.params))
#         return

#     shell.cmdloop()

def main():
    bot = ChatGPT()
    bot.ask("Hello")

if __name__ == "__main__":
    main()
