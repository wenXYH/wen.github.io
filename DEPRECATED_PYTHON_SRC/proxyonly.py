import os
import sys
import time
import json
import codecs
import shutil
import threading
from datetime import datetime, date

if getattr(sys, 'frozen', False):
    rootdir = os.path.dirname(sys.executable)
else:
    rootdir = os.path.dirname(os.path.realpath(__file__))
# make all filenames based on rootdir being unicode
rootdir = rootdir.decode(sys.getfilesystemencoding())
sys.path.append(rootdir)

from lib.utils import init_logging, local_update_datafile, set_ca_certs_env, singleton_check, singleton_clean
from lib.ipc import ActorObject
from component.admin import Admin
from component.circumvention import CircumventionChannel, remote_update_meek_relays
from component.local import HTTPProxy, SocksProxy
from component.matcher import create_matcher, blacklist_info, remote_update_blacklist
from component.brz import able_to_setproxy
from component.hosts import hosts_info, remote_update_hosts
    
class Coordinator(ActorObject):
    def __init__(self, rootdir, conf_file):
        super(Coordinator, self).__init__()
        
        self.rootdir = rootdir
        self.conf_file = conf_file
        
        self.confdata = None
        self.admin = None
        self.cc_channel = None
        self.matcher = None
        self.http_proxy = None
        self.socks_proxy = None
    
    def loadconf(self):
        f = codecs.open(os.path.join(self.rootdir, self.conf_file), "r", "utf-8")
        self.confdata = json.loads(f.read())
        f.close()
        
    def backup_conf(self):
        conf = os.path.join(self.rootdir, self.conf_file)
        shutil.copy(conf, conf + ".last")
        default = conf + ".default"
        if not os.path.isfile(default):
            shutil.copy(conf, default)
    
    def recover_conf(self):
        conf = os.path.join(self.rootdir, self.conf_file)
        shutil.copy(conf + ".last", conf)
        
    def initialize(self):
        self.singleton = singleton_check(self.rootdir)
        if not self.singleton:
            sys.exit(-1)
        
        self.loadconf()
        self.ref().share('rootdir', self.rootdir)
        self.ref().share('confdata', self.confdata)
        self.start_actor()
        
    def start_admin(self):
        self.admin = Admin(self.ref())
        self.admin.start()
        
    def start_cc_channel(self):
        try:
            self.cc_channel = CircumventionChannel(self.ref())
            self.cc_channel.start()
        except Exception, e:
            print "failed to start circumvention channel: %s" % str(e)
            
    def start_local_proxy(self):
        global rootdir
        
        circumvention_url = self.IPC_circumvention_url()
        self.matcher = create_matcher(rootdir, self.confdata, circumvention_url)
        if self.confdata['enable_http_proxy']:
            try:
                self.http_proxy = HTTPProxy(self.ref(), self.matcher)
                self.http_proxy.start()
            except Exception, e:
                print "failed to start http proxy: %s" % str(e)
        
        if self.confdata['enable_socks_proxy']:
            try:
                self.socks_proxy = SocksProxy(self.ref(), self.matcher)
                self.socks_proxy.start()
            except Exception, e:
                print "failed to start socks proxy: %s" % str(e)
                
    def proxy_info(self):
        if self.socks_proxy:
            # ip, port = self.socks_proxy.ref().IPC_addr()
            # return ProxyInfo(socks.PROXY_TYPE_SOCKS5, ip, port, True, None, None)
            url = self.socks_proxy.ref().IPC_url()
            return {'http': url, 'https': url}
        elif self.http_proxy:
            # ip, port = self.http_proxy.ref().IPC_addr()
            # return ProxyInfo(socks.PROXY_TYPE_HTTP, ip, port, True, None, None)
            url = self.http_proxy.ref().IPC_url()
            return {'http': url, 'https': url}
        else:
            # return None
            return {}
                
    def update_matcher(self):
        circumvention_url = self.IPC_circumvention_url()
        self.matcher = create_matcher(rootdir, self.confdata, circumvention_url)
        if self.http_proxy:
            self.http_proxy.ref().IPC_update_matcher(self.matcher)
        if self.socks_proxy:
            self.socks_proxy.ref().IPC_update_matcher(self.matcher)
            
    def check_and_update_blacklist(self):
        try:
            blacklist_date = datetime.strptime(self.matcher.blacklist_matcher.meta['date'], '%Y-%m-%d').date()
            if date.today() > blacklist_date:
                updated = remote_update_blacklist(self.proxy_info(), self.rootdir, self.confdata)
                if updated:
                    self.update_matcher()
        except Exception, e:
            print "failed to update blacklist: %s" % str(e)
            
    def check_and_update_hosts(self):
        try:
            hosts_date = datetime.strptime(self.matcher.hosts.meta['date'], '%Y-%m-%d').date()
            if date.today() > hosts_date:
                updated = remote_update_hosts(self.proxy_info(), self.rootdir, self.confdata)
                if updated:
                    self.update_matcher()
        except Exception, e:
            print "failed to update hosts: %s" % str(e)
            
    def update_meek_relays(self):
        try:
            updated = remote_update_meek_relays(self.proxy_info(), self.rootdir, self.confdata)
            if updated:
                self.cc_channel.ref().IPC_update_meek_relays()
        except Exception, e:
            print "failed to update meek relays: %s" % str(e)
        
    def check_for_update(self):
        time.sleep(20)
        if self.cc_channel.type == "meek":
            self.update_meek_relays()
        self.check_and_update_blacklist()
        self.check_and_update_hosts()
        
    def run(self):
        try:
            self.initialize()
            self.start_cc_channel()
            self.start_admin()
            self.start_local_proxy()
        except Exception, e:
            print "failed to start basic steps/processes: %s, try to recover ..." % str(e)
            if not self.recover_conf():
                raise e
            
            self.end()
            self.initialize()
            self.start_cc_channel()
            self.start_admin()
            self.start_local_proxy()
        
        self.backup_conf()
        t = threading.Thread(target=self.check_for_update)
        t.daemon = True
        t.start()
        
    def end(self):
        if self.admin:
            self.admin.terminate()
            self.admin.join()
        if self.cc_channel:
            self.cc_channel.terminate()
            self.cc_channel.join()
        if self.http_proxy:
            self.http_proxy.terminate()
            self.http_proxy.join()
        if self.socks_proxy:
            self.socks_proxy.terminate()
            self.socks_proxy.join()
        singleton_clean(self.rootdir, self.singleton)
            
    # IPC interfaces
    def IPC_circumvention_url(self):
        """ask circumvention channel for forwarding url"""
        return self.cc_channel.ref().IPC_url()
    
    def IPC_socks_proxy_addr(self):
        return self.socks_proxy.ref().IPC_addr()
    
    def IPC_http_proxy_addr(self):
        return self.http_proxy.ref().IPC_addr()
            
    def IPC_shadowsocks_methods(self):
        return self.cc_channel.ref().IPC_shadowsocks_methods()
    
    def IPC_blacklist_info(self):
        return blacklist_info(self.rootdir, self.confdata, self.matcher.blacklist_matcher)
        
    def IPC_hosts_info(self):
        return hosts_info(self.rootdir, self.confdata, self.matcher.hosts)
        
    def IPC_get_custom_blacklist(self):
        return self.matcher.blacklist_matcher.get_custom_blacklist()
    
    def IPC_get_custom_whitelist(self):
        return self.matcher.blacklist_matcher.get_custom_whitelist()
    
    def IPC_update_config(self, data):
        try:
            self.confdata.update(data)
            f = codecs.open(os.path.join(self.rootdir, self.conf_file), "w", "utf-8")
            f.write(json.dumps(self.confdata,
                        sort_keys=True,
                        indent=4,
                        separators=(',', ': '),
                        ensure_ascii=False))
            f.close()
            return data
        except Exception, e:
            print "failed to update config: %s" % str(e)
            return None
        
    def IPC_resume_default_config(self):
        conf = os.path.join(self.rootdir, self.conf_file)
        shutil.copy(conf + ".default", conf)
        self.loadconf()
        return self.confdata
    
    def IPC_update_blacklist(self):
        try:
            updated = remote_update_blacklist(self.proxy_info(), self.rootdir, self.confdata)
            if updated:
                self.update_matcher()
            return True
        except Exception, e:
            print "failed to update blacklist: %s" % str(e)
            return False
        
    def IPC_update_custom_list(self, custom_bl=None, custom_wl=None):
        if custom_bl:
            local_update_datafile(u"\n".join(custom_bl),
                os.path.join(self.rootdir, self.confdata['custom_blacklist']))
        if custom_wl:
            local_update_datafile(u"\n".join(custom_wl),
                os.path.join(self.rootdir, self.confdata['custom_whitelist']))
        self.update_matcher()
        
    def IPC_update_hosts(self):
        try:
            updated = remote_update_hosts(self.proxy_info(), self.rootdir, self.confdata)
            if updated:
                self.update_matcher()
            return True
        except Exception, e:
            print "failed to update hosts: %s" % str(e)
            return False 
        
    def IPC_update_hosts_disabled(self, disabled):
        local_update_datafile(u"\n".join(disabled), os.path.join(self.rootdir, self.confdata['hosts']['disabled']))
        self.update_matcher()
        
    def IPC_support_ssh(self):
        return self.cc_channel.ref().IPC_support_ssh()
    
    def IPC_setproxy_tip(self):
        if not self.confdata['launch_browser']:
            return False
        return not able_to_setproxy()
        
def close_std():
    sys.stdin.close()
    sys.stdin = open(os.devnull)
    sys.stderr.close
    sys.stderr = open(os.devnull)
        
def main():
    init_logging() 
    
    global rootdir
    conf_file = "config.json"
    set_ca_certs_env(os.path.join(rootdir, "cacert.pem").encode(sys.getfilesystemencoding()))
    coordinator = Coordinator(rootdir, conf_file)
    coordinator.run()
    try:
        while True:
            time.sleep(10)
    except:
        print "quit ..."
        coordinator.end()
    
if __name__ == '__main__':
    main()
