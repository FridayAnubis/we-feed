"""Prefer the mobile WeRead workflow when credentials and a convertible feed exist."""

from core.wx.model.weread_mp import MpsWereadMP, WereadMPAPIError, mp_book_id
from core.print import print_warning
from contextvars import ContextVar

history_attempted = ContextVar("weread_history_attempted", default=False)


class MpsHistoryFirst:
    def __init__(self, fallback):
        self.fallback = fallback
        self.active = fallback

    def __getattr__(self, name):
        return getattr(self.active, name)

    def get_Articles(self, faker_id=None, Mps_id=None, Mps_title="", CallBack=None,
                     start_page=0, MaxPage=1, interval=10, Gather_Content=False,
                     Item_Over_CallBack=None, Over_CallBack=None):
        args = dict(faker_id=faker_id, Mps_id=Mps_id, Mps_title=Mps_title,
                    CallBack=CallBack, start_page=start_page, MaxPage=MaxPage,
                    interval=interval, Gather_Content=Gather_Content,
                    Item_Over_CallBack=Item_Over_CallBack, Over_CallBack=Over_CallBack)
        attempted = False
        if mp_book_id(Mps_id) or mp_book_id(faker_id):
            mobile = MpsWereadMP()
            mobile._load_weread_auth()
            if mobile._weread_cookies:
                attempted = True
                self.active = mobile
                try:
                    # The wrapper owns completion, including fallback after total failure.
                    mobile.get_Articles(**{**args, "Over_CallBack": None})
                except Exception as exc:
                    if mobile.response_valid or not isinstance(exc, WereadMPAPIError):
                        if Over_CallBack:
                            Over_CallBack(mobile.articles)
                        raise
                    print_warning(f"微信读书工作流未取得文章({exc.code})，回退原采集模式")
                else:
                    if Over_CallBack:
                        Over_CallBack(mobile.articles)
                    return
                finally:
                    client = getattr(mobile, "_weread_client", None)
                    if client:
                        client.close()
        self.active = self.fallback
        token = history_attempted.set(attempted)
        try:
            return self.fallback.get_Articles(**args)
        finally:
            history_attempted.reset(token)
