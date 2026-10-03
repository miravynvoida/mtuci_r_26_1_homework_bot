import os
import uvicorn
from web_app import app

if __name__ == '__main__':
    uvicorn.run(app, host=os.getenv('WEB_HOST', '0.0.0.0'), port=int(os.getenv('PORT', os.getenv('WEB_PORT', '3000'))), log_level='info', access_log=False)
