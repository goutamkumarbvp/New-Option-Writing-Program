import time
class Watchdog:
 def __init__(self,timeout=5):self.timeout=timeout;self.last=time.time()
 def beat(self):self.last=time.time()
 def healthy(self):return time.time()-self.last<=self.timeout
class KillSwitch:
 def __init__(self):self.triggered=False;self.reason=''
 def trigger(self,reason):self.triggered=True;self.reason=reason
 def reset(self):self.triggered=False;self.reason=''
