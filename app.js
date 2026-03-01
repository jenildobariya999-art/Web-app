let transactions = [
{user:"Rahul", type:"credit", amount:200},
{user:"Aman", type:"debit", amount:100},
]

let list = document.getElementById("txlist")

transactions.forEach(t => {

let li = document.createElement("li")

li.innerText =
t.user + " " + t.type + " ₹" + t.amount

list.appendChild(li)

})

function addFund(){

alert("Choose Method\nUPI\nVSV\nSaathi Pay\nUltra Pay")

}
