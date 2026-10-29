# Movie Magic – cloud movie ticket booking

Flask backend · Amazon DynamoDB · Amazon SNS e-mail confirmations · hosted on AWS EC2.

## Features
- Register / log in (hashed passwords, session auth)
- Browse movies and live events; search by title, genre, language, city
- Showtimes by day and theatre with live "seats left"
- Interactive seat map (8 rows × 12, recliner rows G–H cost more), live updates every 5 s
- **Atomic booking**: DynamoDB conditional update means two people can never get the same seat
- Booking confirmation e-mail via **SNS** (movie, date, time, seats, amount)
- "My bookings" ticket stubs; admin dashboard (stats, add movie/event, all bookings)
- `/health` endpoint for load balancers / monitoring; logs go to stdout (CloudWatch-ready)

## Run locally (no AWS needed)
```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # USE_AWS=false by default
python app.py                                         # http://localhost:5000
```
Local mode stores data in `data/local_db.json` and shows the e-mail as a preview on the booking page.
Demo admin: `admin@moviemagic.com` / `admin123` (change it!).

## Deploy on AWS
1. **IAM role** for the EC2 instance with this policy (least privilege):
```json
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["dynamodb:CreateTable","dynamodb:DescribeTable","dynamodb:ListTables","dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:Scan","dynamodb:BatchWriteItem"],
  "Resource":"arn:aws:dynamodb:*:*:table/MovieMagic_*"},
 {"Effect":"Allow","Action":["dynamodb:ListTables"],"Resource":"*"},
 {"Effect":"Allow","Action":["sns:CreateTopic","sns:Subscribe","sns:Publish"],"Resource":"arn:aws:sns:*:*:MovieMagicNotifications"}]}
```
2. **Launch EC2** (Ubuntu 22.04, t2/t3.micro), attach the role, security group: 22 (your IP), 80/5000.
3. **Install & run**
```bash
sudo apt update && sudo apt install -y python3-venv git
# copy the project to /home/ubuntu/moviemagic (scp / git clone)
cd moviemagic && python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env      # USE_AWS=true, AWS_REGION, SECRET_KEY, ADMIN_EMAILS
gunicorn -w 3 -b 0.0.0.0:5000 app:app  # open http://<EC2-public-IP>:5000
```
   On first start the four DynamoDB tables are created and seeded automatically
   (`MovieMagic_Users`, `_Movies`, `_Shows`, `_Bookings`) and the SNS topic is created.
4. **Keep it running** – `/etc/systemd/system/moviemagic.service`:
```ini
[Unit]
Description=Movie Magic
After=network.target
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/moviemagic
EnvironmentFile=/home/ubuntu/moviemagic/.env
ExecStart=/home/ubuntu/moviemagic/venv/bin/gunicorn -w 3 -b 0.0.0.0:80 app:app
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
[Install]
WantedBy=multi-user.target
```
`sudo systemctl enable --now moviemagic`

## Important SNS note
SNS e-mail subscriptions must be **confirmed**. Each user is subscribed when they register and gets an
"AWS Notification – Subscription Confirmation" e-mail; they click *Confirm subscription* once. A filter policy
makes sure each person only receives their own tickets from the shared topic.

## Project layout
```
app.py        routes, auth, booking logic          db.py      LocalStore + DynamoStore, seed data
notify.py     SNS / local e-mail                   templates/ Jinja pages      static/ CSS + seat-map JS
```
## Scaling ideas
Scans are fine for a demo; for production add GSIs (Shows by city+date, Bookings by email), put an ALB
and Auto Scaling group in front of EC2, move secrets to SSM Parameter Store, and use SES for branded HTML e-mails.
