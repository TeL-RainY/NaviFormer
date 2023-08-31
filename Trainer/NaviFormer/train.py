import torch, tqdm, os, datetime
import torch.utils.tensorboard as tensorboard
import torch.distributed as dist
from torch.utils.data import DataLoader as DL
from torch.utils.data.distributed import DistributedSampler as DS
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.cuda.amp import autocast
from torch.cuda.amp import GradScaler

from data import NaviDataset, NaviCollateFN
from nn import NaviGPT, GPT2Config

def init_DDP(backend="nccl"):
    dist.init_process_group(backend=backend, timeout=datetime.timedelta(seconds=3600*4))
    rank = dist.get_rank()
    world_size = dist.get_world_size()
    device = torch.device("cuda:{}".format(rank))
    torch.cuda.set_device(rank)
    return rank, world_size, device

class Train:
    def __init__(self, model, dataloader, optimizer, scaler, scheduler, epoch=10000, grad_norm_clip=None, fine_tune=None, name="model", rank=0, device="cuda", world_size=1):
        self.model = model
        self.dataloader = dataloader
        self.optimizer = optimizer
        self.scaler = scaler
        self.scheduler = scheduler
        self.epoch = epoch
        self.grad_norm_clip = grad_norm_clip
        if fine_tune != None:
            self.model.module.load_state_dict(torch.load(fine_tune))
        self.tb_writer = None
        self.name = name
        self.rank = rank
        self.device = device
        self.world_size = world_size

    def __run(self, data, epoch)->None:
        self.model.train()
        self.optimizer.zero_grad()
        with autocast():
            loss = self.model.module.learn(*data)
        self.scaler.scale(loss).backward()
        if self.grad_norm_clip != None:
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.grad_norm_clip)
        self.scaler.step(self.optimizer)
        self.scaler.update()
        self.scheduler.step()
        if self.tb_writer != None:
            self.tb_writer.add_scalar('Loss', loss.item(), epoch)

    def Run(self)->None:
        if self.rank == 0:
            print('--Training Process Running:', flush=True)
            if not os.path.exists('./log'): os.mkdir('./log')
            if not os.path.exists('./log/{}'.format(self.name)): os.mkdir('./log/{}'.format(self.name))
            self.tb_writer = tensorboard.SummaryWriter(log_dir='./log/{}/tfboard'.format(self.name))
        dist.barrier()

        data_iter = iter(self.dataloader)
        epoch_iter = range(self.epoch) if self.rank != 0 else tqdm.tqdm(range(self.epoch), '', ncols=120, unit='Epoch')
        for epoch in epoch_iter:
            data = next(data_iter, None)
            if data is None:
                data_iter = iter(self.dataloader)
                data = next(data_iter)
            for i in range(len(data)):
                data[i] = data[i].cuda(device=self.device, non_blocking=True)
            self.__run(data, epoch)
            if (self.rank == 0) and ((epoch+1) % 2000 == 0):
                torch.save(self.model.module.state_dict(), './log/{}/'.format(self.name)+str(int((epoch+1)/1000))+'K.pt')
            dist.barrier()

        if self.rank == 0:
            torch.save(self.model.module.state_dict(), './log/{}/last_model.pt'.format(self.name))
            print('--Finish: Models are saved in ./log/{} .'.format(self.name), flush=True)
        dist.barrier()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", type=str, default="model")
    parser.add_argument("--fine_tune", type=str, default=None)
    parser.add_argument("--dataset", type=str, default="mongodb://localhost:27017/")
    parser.add_argument("--max_len", type=int, default=8)
    parser.add_argument("--token_dim", type=int, default=768)
    parser.add_argument("--nlayer", type=int, default=12)
    parser.add_argument("--nhead", type=int, default=12)
    parser.add_argument("--ninner", type=int, default=768*4)
    parser.add_argument("--dropout", type=float, default=0.01)
    parser.add_argument("--lr", type=float, default=10**-4)
    parser.add_argument("--weight_decay", type=float, default=10**-4)
    parser.add_argument("--warmup_steps", type=int, default=10000)
    parser.add_argument("--epoch", type=int, default=50000)
    parser.add_argument("--batch", type=int, default=128)
    parser.add_argument("--grad_norm_clip", type=float, default=None)
    parser.add_argument("--checkpoint", type=bool, default=True)
    parse = parser.parse_args()

    torch.backends.cudnn.enable=True
    torch.backends.cudnn.benchmark=True

    rank, world_size, device = init_DDP()
    dataset = NaviDataset(parse.dataset)
    sampler = DS(dataset=dataset, shuffle=True, drop_last=True)
    collate = NaviCollateFN(parse.max_len)
    dataloader = DL(dataset=dataset, batch_size=parse.batch, num_workers=world_size, sampler=sampler, collate_fn=collate.collate_fn, drop_last=True, pin_memory=True)
    config = GPT2Config(n_embd=parse.token_dim, n_layer=parse.nlayer, n_head=parse.nhead, n_inner=parse.ninner, resid_pdrop=parse.dropout)
    model = DDP(NaviGPT(config, parse.checkpoint).to(device), device_ids=[rank], output_device=rank)
    optimizer = torch.optim.AdamW(model.parameters(), lr=parse.lr, weight_decay=parse.weight_decay)
    scaler = GradScaler()
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda steps: min((steps+1)/parse.warmup_steps, 1))
    train = Train(model, dataloader, optimizer, scaler, scheduler, parse.epoch, parse.grad_norm_clip, parse.fine_tune, parse.name, rank, device, world_size)
    train.Run()
