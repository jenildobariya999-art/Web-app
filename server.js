const express = require("express")
const cors = require("cors")
const fs = require("fs")
const TelegramBot = require("node-telegram-bot-api")

const app = express()
app.use(cors())
app.use(express.json())

const BOT_TOKEN = "8327459100:AAF7jvww4apf0grc66Nd--PBTkkk3EpyC2E"
const ADMIN_ID = "6925391837"

const bot = new TelegramBot(BOT_TOKEN)

let db = JSON.parse(fs.readFileSync("database.json"))

function save(){
fs.writeFileSync("database.json",JSON.stringify(db,null,2))
}

app.post("/login",(req,res)=>{

let id = req.body.id

if(!db.users[id]){
db.users[id] = {balance:0}
save()
}

res.json(db.users[id])

})

app.post("/deposit",(req,res)=>{

let {id, amount, utr, method} = req.body

db.deposits.push({
id,
amount,
utr,
method,
status:"pending"
})

save()

bot.sendMessage(ADMIN_ID,
`New Deposit

User: ${id}
Amount: ₹${amount}
Method: ${method}
UTR: ${utr}`)

res.json({ok:true})

})

app.get("/deposits",(req,res)=>{
res.json(db.deposits)
})

app.post("/approve-deposit",(req,res)=>{

let index = req.body.index
let dep = db.deposits[index]

if(dep.status !== "pending") return res.send("done")

db.users[dep.id].balance += Number(dep.amount)

dep.status="approved"

db.transactions.push({
user:dep.id,
type:"deposit",
amount:dep.amount
})

save()

bot.sendMessage(dep.id,
`✅ Deposit Approved

Amount: ₹${dep.amount}`)

res.json({ok:true})

})

app.post("/withdraw",(req,res)=>{

let {id, amount, upi} = req.body

db.withdraws.push({
id,
amount,
upi,
status:"pending"
})

save()

bot.sendMessage(ADMIN_ID,
`Withdraw Request

User ${id}
Amount ₹${amount}
UPI ${upi}`)

res.json({ok:true})

})

app.get("/balance/:id",(req,res)=>{
res.json({balance:db.users[req.params.id]?.balance || 0})
})

app.listen(3000,()=>console.log("Server running"))
